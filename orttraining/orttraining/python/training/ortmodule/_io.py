# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import copy
import gc
import inspect
import warnings
from collections import abc
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import onnx
import torch

from ._fallback import ORTModuleIOError, ORTModuleONNXModelException, wrap_exception
from ._logger import LogLevel, suppress_os_stream_output
from ._runtime_inspector import RuntimeInspector
from ._utils import warn_of_constant_inputs

_ModelInputOutputType = Union[
    None,
    str,
    int,
    bool,
    float,
    torch.Tensor,
    Sequence["_ModelInputOutputType"],
    Mapping[str, "_ModelInputOutputType"],
]


class _TensorStub:
    """Tensor stub class used to represent model's input or output"""

    __slots__ = ["name", "dtype", "shape", "shape_dims"]

    def __init__(
        self, name: Optional[str] = None, dtype: Optional[str] = None, shape=None, shape_dims: Optional[int] = None
    ):
        self.name: Optional[str] = name
        self.dtype: Optional[str] = dtype
        self.shape = shape
        self.shape_dims: Optional[int] = shape_dims  # r.g. rank.

    def __repr__(self) -> str:
        result = "_TensorStub("
        if self.name is not None:
            result += f"name={self.name}"
        if self.dtype is not None:
            if result[-1] != "(":
                result += ", "
            result += f"dtype={self.dtype}"
        if self.shape is not None:
            if result[-1] != "(":
                result += ", "
            result += f"shape={self.shape}"
        if self.shape_dims is not None:
            if result[-1] != "(":
                result += ", "
            result += f"shape_dims={self.shape_dims}"
        result += ")"
        return result

    def __eq__(self, other):
        if not other:
            return False
        elif not isinstance(other, _TensorStub):
            raise NotImplementedError("_TensorStub must only be compared to another _TensorStub instance!")
        elif self.name != other.name:
            return False
        elif self.dtype != other.dtype:
            return False
        elif self.shape != other.shape:
            return False
        elif self.shape_dims != other.shape_dims:
            return False
        return True


_ModelInputOutputSchemaType = Union[
    None,
    str,
    _TensorStub,
    Sequence["_ModelInputOutputSchemaType"],
    Mapping[str, "_ModelInputOutputSchemaType"],
]


class _OutputIdentityOp(torch.autograd.Function):
    """Internal class used to prepend Identity ops in model's outputs

    This class is required to support ONNX models which passthrough [some of] the models's inputs
    directly to the graph output. This is an issue because ONNX Runtime cannot build proper
    gradient graph based on this pattern.

    Adding a direct Identity Op to the user model doesn't work as the ONNX exporter would optimize it away,
    resulting in the same issue.

    Therefore a custom Autograd function was introduced to add an Identity right before the output
    in a way the ONNX exporter will not optimize it away.

    Given the model below

    .. code-block:: python

        class PassthroughNet(torch.nn.Module):
            def __init__(self, input_size, hidden_size, num_classes):
                super(PassthroughNet, self).__init__()
                self.fc1_1 = torch.nn.Linear(input_size, hidden_size)
                self.relu1 = torch.nn.ReLU()
                self.fc1_2 = torch.nn.Linear(hidden_size, num_classes)
            def forward(self, input1, passthrough_input):
                out1 = self.fc1_2(self.relu1(self.fc1_1(input1)))
                # use shape from passthrough_input
                out1 = out1.view(passthrough_input.size()[0], -1)
                return out1, passthrough_input

    We can see `passthrough_input` is part of both model input and output and the resulting
    ONNX subgraph would contain something like `output2 -> output2`.

    By prepending each model output to an :class:`_OutputIdentityOp` op, the resulting
    onnx subgraph for this example would be  `passthrough_input -> Identity -> output2`.

    TODO: Remove once PyTorch 1.8.2 or newer is released
    """

    @staticmethod
    def forward(ctx, input):
        return torch.nn.Identity()(input)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output

    @staticmethod
    def symbolic(g, self):
        return g.op("Identity", self)


class _PrimitiveType:
    _primitive_types = {int, bool, float}

    @staticmethod
    def is_primitive_type(value):
        return type(value) in _PrimitiveType._primitive_types

    @staticmethod
    def get_tensor(value, device) -> torch.Tensor:
        return torch.tensor(value, device=device)

    @staticmethod
    def get_primitive_dtype(value):
        # If `value` is a boolean, save the value of the boolean in dtype.
        # This way, if the value changes from one forward call to the next, the schema will mismatch,
        # and the model will be re-exported.
        return f"{str(type(value))}_{value}" if isinstance(value, bool) else str(type(value))


def flatten_kwargs(kwargs, device):
    def _flatten_kwargs(value, name):
        if _PrimitiveType.is_primitive_type(value):
            flattened_kwargs[name] = _PrimitiveType.get_tensor(value, device)
        elif isinstance(value, torch.Tensor):
            flattened_kwargs[name] = value
        elif isinstance(value, abc.Sequence):
            # If the input is a sequence (like a list), expand the list so that
            # each element of the list has a corresponding entry in the flattened
            # kwargs dict
            for idx, val in enumerate(value):
                _flatten_kwargs(val, f"{name}_{idx}")
        elif isinstance(value, abc.Mapping):
            # If the input is a mapping (like a dict), expand the dict so that
            # each element of the dict has an entry in the flattened kwargs dict
            for key, val in value.items():
                _flatten_kwargs(val, f"{name}_{key}")

    flattened_kwargs = {}
    for key, value in kwargs.items():
        _flatten_kwargs(value, key)

    return flattened_kwargs


class _InputInfo:
    def __init__(
        self,
        names: List[str],
        shape: List[List[int]],
        require_grad_names: Optional[List[str]] = None,
        dynamic_axes: Optional[Dict[str, Dict[int, str]]] = None,
        schema: Optional[_ModelInputOutputSchemaType] = None,
        num_positionals=0,
    ):
        self.names: List[str] = names
        self.shape: List[List[int]] = shape
        self.require_grad_names: List[str] = require_grad_names if require_grad_names else []
        self.dynamic_axes: Dict[str, Dict[int, str]] = dynamic_axes if dynamic_axes else {}
        self.schema: _ModelInputOutputSchemaType = schema if schema else []
        self.num_positionals = num_positionals
        self.kwargs = None

    def __repr__(self) -> str:
        return f"""_InputInfo class:
            \tNames:                            {self.names}
            \tShape:                            {self.shape}
            \tRequire gradient:                 {self.require_grad_names}
            \tDynamic axes:                     {self.dynamic_axes}
            \tSchema:                           {self.schema}
            \t#Positionals (total):             {self.num_positionals}"""

    def flatten(
        self, args: Sequence[_ModelInputOutputType], kwargs: Mapping[str, _ModelInputOutputType], device: torch.device
    ) -> Sequence[_ModelInputOutputType]:
        """Flatten args and kwargs in a single tuple of tensors with strict ordering"""

        ret = [_PrimitiveType.get_tensor(arg, device) if _PrimitiveType.is_primitive_type(arg) else arg for arg in args]
        flattened_kwargs = flatten_kwargs(kwargs, device)
        ret += [flattened_kwargs[name] for name in self.names if name in flattened_kwargs]
        self.kwargs = kwargs

        # if kwargs is empty, append an empty dictionary at the end of the sample inputs to make exporter
        # happy. This is because the exporter is confused with kwargs and dictionary inputs otherwise.
        if not kwargs:
            ret.append({})

        return ret

    def unflatten(
        self, flat_args: Sequence[_ModelInputOutputType]
    ) -> Tuple[Sequence[_ModelInputOutputType], Mapping[str, _ModelInputOutputType]]:
        """Unflatten tuple of tensors into args and kwargs"""

        args = tuple(flat_args[: self.num_positionals])
        kwargs = self.kwargs
        self.kwargs = None
        return args, kwargs


def _combine_input_buffers_initializers(
    params: List[torch.nn.parameter.Parameter],
    onnx_input_names: List[str],
    input_info: Optional[_InputInfo],
    named_buffer: Iterator[Tuple[str, torch.Tensor]],
    inputs: Sequence[_ModelInputOutputType],
    kwargs: Mapping[str, _ModelInputOutputType],
    device: torch.device,
    rt_inspector: RuntimeInspector,
):
    """Creates forward `*inputs` list from user input and PyTorch initializers

    ONNX Runtime forward requires an ordered list of:
        * User input: computed from forward InferenceSession
        * Initializers: computed from original PyTorch model parameters.
    """

    def _expand_inputs(current_input, non_none_inputs, name=""):
        # The exporter handles input lists by expanding them so that each
        # element of the list is its own input.
        # ORTModule must match this behavior by also expanding the inputs.
        if current_input is None or isinstance(current_input, str):
            # Drop all None and string inputs
            return
        if isinstance(current_input, abc.Sequence):
            # If the input is a sequence (like a list), expand the list so that
            # each element of the list is an input by itself
            for i, inp in enumerate(current_input):
                _expand_inputs(inp, non_none_inputs, f"{name}_{i}" if name else str(i))
        elif isinstance(current_input, abc.Mapping):
            # If the input is a mapping (like a dict), expand the dict so that
            # each element of the dict is an input by itself
            for key, val in current_input.items():
                _expand_inputs(val, non_none_inputs, f"{name}_{key}" if name else key)
        else:
            # else just collect all the non none inputs within non_none_inputs
            if isinstance(non_none_inputs, abc.Sequence):
                non_none_inputs.append(current_input)
            elif isinstance(non_none_inputs, abc.Mapping):
                non_none_inputs[name] = current_input

    # User inputs
    non_none_inputs = []
    _expand_inputs(inputs, non_none_inputs)
    flattened_kwargs_inputs = {}
    _expand_inputs(kwargs, flattened_kwargs_inputs)
    buffer_names_dict = {buffer_name: inp for buffer_name, inp in named_buffer}
    result = []
    embed_sparsity_results = []
    label_sparsity_results = []

    for input_idx, name in enumerate(onnx_input_names):
        inp = None
        if name in flattened_kwargs_inputs and flattened_kwargs_inputs[name] is not None:
            # Only use keywords coming from user that are expected by ONNX model
            inp = flattened_kwargs_inputs[name]

        if inp is None:
            try:
                # Only use positionals coming from user that are expected by ONNX model
                # if input_idx >= len(input_info.names), IndexError will be thrown
                if name != input_info.names[input_idx]:
                    # When ONNX drops unused inputs, get correct index from user input
                    # if name is not in input_info.names, ValueError will be thrown
                    input_idx = input_info.names.index(name)  # noqa: PLW2901
                inp = non_none_inputs[input_idx]
            except (IndexError, ValueError):
                # ONNX input name is not present in input_info.names.
                pass

        if inp is None:
            # Registered buffers are translated to user_input+initializer in ONNX
            try:  # noqa: SIM105
                inp = buffer_names_dict[name]
            except KeyError:
                # ONNX input name is not present in the registered buffer dict.
                pass

        if inp is not None:
            if _PrimitiveType.is_primitive_type(inp):
                inp = _PrimitiveType.get_tensor(inp, device)

            found, embedding_is_sparse, label_is_sparse = rt_inspector.inspect_input(name, inp)
            if found:
                if embedding_is_sparse is True:
                    embed_sparsity_results.append(name)
                if label_is_sparse is True:
                    label_sparsity_results.append(name)
            result.append(inp)
        else:
            raise wrap_exception(
                ORTModuleONNXModelException, RuntimeError(f"Input is present in ONNX graph but not provided: {name}.")
            )

    # params is a list of all initializers known to the onnx graph
    result.extend(params)

    return result, embed_sparsity_results, label_sparsity_results


def deepcopy_model_input(
    *args, **kwargs
) -> Tuple[Sequence[_ModelInputOutputType], Mapping[str, _ModelInputOutputType]]:
    def extract_tensor(value):
        if isinstance(value, torch.Tensor):
            if value.requires_grad:
                return value.data.requires_grad_()
            else:
                return value.data
        else:
            return value

    sample_args_copy: Sequence[_ModelInputOutputType] = [extract_tensor(value) for value in args]
    sample_args_copy = copy.deepcopy(tuple(sample_args_copy))

    sample_kwargs_copy: Mapping[str, _ModelInputOutputType] = {}
    for name, value in kwargs.items():
        sample_kwargs_copy[name] = extract_tensor(value)
    sample_kwargs_copy = copy.deepcopy(sample_kwargs_copy)

    return sample_args_copy, sample_kwargs_copy


def unflatten_user_output(output_schema: Optional[_ModelInputOutputSchemaType], outputs):
    """Follows the schema to generate an output that is expected by the user"""

    def _replace_stub_with_tensor_value(user_output, outputs, output_idx):
        # Recursively traverse across user_output and replace all _TensorStub
        # with torch.Tensor values from outputs following output_idx

        if user_output is None:
            return None
        elif isinstance(user_output, _TensorStub):
            out = outputs[output_idx[0]]
            output_idx[0] += 1
            return out

        if isinstance(user_output, abc.Sequence):
            sequence_type = type(user_output)
            if hasattr(sequence_type, "_make"):  # namedtuple
                sequence_type = type(user_output)
                user_output = sequence_type._make(
                    _replace_stub_with_tensor_value(uo, outputs, output_idx) for uo in user_output
                )
            else:
                user_output = sequence_type(
                    _replace_stub_with_tensor_value(uo, outputs, output_idx) for uo in user_output
                )
        elif isinstance(user_output, abc.Mapping):
            new_user_output = copy.copy(user_output)
            for key in sorted(user_output):
                new_user_output[key] = _replace_stub_with_tensor_value(new_user_output[key], outputs, output_idx)
            user_output = new_user_output
        else:
            raise wrap_exception(
                ORTModuleIOError,
                TypeError(f"ORTModule does not support the following model output type {type(user_output)}."),
            )

        return user_output

    # It is expected that the outputs are ordered in the way defined in the exported onnx model
    # which is the order in which the output schema was saved.
    output_idx = [0]
    user_output = _replace_stub_with_tensor_value(output_schema, outputs, output_idx)
    return user_output


def _extract_schema(data: _ModelInputOutputType) -> _ModelInputOutputSchemaType:
    """Extract the data schema by replacing every torch.Tensor value with _TensorStub.

    Depth first traversal to iterate over the data:
    > Replace every tensor with a stub
    > Replace None/str typed data with itself
    > Recreate tensor from data for other primitive types, and replace them with a stub.

    TODO(pengwa): use a unified logger and log level control.
    Examples:
        Example 1, list:
            data = [torch.tensor(1), torch.tensor(2)]
            schema = [_TensorStub(shape=()), _TensorStub(shape=())]

        Example 2, dict:
            data = {"a": torch.tensor(1), "b": torch.tensor(2)}
            schema = {"a": _TensorStub(shape=()), "b": _TensorStub(shape=())}

        Example 3, dict of list:
            data = {"a": [torch.tensor(1), torch.tensor(2)], "b": [torch.tensor(3), torch.tensor(4)]}
            schema = {"a": [_TensorStub(shape=()), _TensorStub(shape=())],
                        "b": [_TensorStub(shape=()), _TensorStub(shape=())]}

        Example 4, nested dict:
            data = {"a": {"b": torch.tensor(1), "c": torch.tensor(2)},
                    "d": {"e": torch.tensor(3), "f": torch.tensor(4)}}
            schema = {"a": {"b": _TensorStub(shape=()), "c": _TensorStub(shape=())},
                        "d": {"e": _TensorStub(shape=()), "f": _TensorStub(shape=())}}

        Example 5, dict of mixed list and dict:
            data = {"a": [torch.tensor(1), torch.tensor(2)], "b": {"c": torch.tensor(3), "d": torch.tensor(4)}}
            schema = {"a": [_TensorStub(shape=()), _TensorStub(shape=())],
                        "b": {"c": _TensorStub(shape=()), "d": _TensorStub(shape=())}}

    Args:
        data: The data to extract the schema from, which can be in any kind of nested structure,
            including Sequence and Mapping.


    Returns:
        The schema of the data, which has the same structure as the data.

    """

    if data is None:
        return data
    elif isinstance(data, str):
        warn_of_constant_inputs(data)
        return data
    elif _PrimitiveType.is_primitive_type(data):
        if isinstance(data, bool):
            warn_of_constant_inputs(data)
        return _TensorStub(dtype=_PrimitiveType.get_primitive_dtype(data), shape_dims=0)
    # Depth first traversal to iterate over the data to replace every tensor with a stub
    elif isinstance(data, torch.Tensor):
        return _TensorStub(dtype=str(data.dtype), shape_dims=len(data.size()))

    # Instead of replacing the tensor with a stub in the original user input, build the stubbed_schema
    # from scratch from the user input.
    stubbed_schema: Optional[_ModelInputOutputSchemaType] = None
    if isinstance(data, abc.Sequence):
        sequence_type = type(data)
        stubbed_schema = [_extract_schema(val) for val in data]
        try:
            # namedtuple can be created by passing the list sequence to method _make
            stubbed_schema = sequence_type._make(stubbed_schema)
        except AttributeError:
            # If attribute error encountered, create the sequence directly
            stubbed_schema = sequence_type(stubbed_schema)
    elif isinstance(data, abc.Mapping):
        dict_type = type(data)
        stubbed_schema = {key: _extract_schema(data[key]) for key in data}
        stubbed_schema = dict_type(**stubbed_schema)
    else:
        raise wrap_exception(
            ORTModuleIOError, TypeError(f"ORTModule does not support the following model data type {type(data)}")
        )
    return stubbed_schema


def _parse_outputs_and_extract_names_and_dynamic_axes(module_output) -> Tuple[List[str], Dict[str, Dict[int, str]]]:
    """Parses through the module output and returns output names and dynamic axes"""

    def _populate_output_names_and_dynamic_axes(
        output, output_names: List[str], output_dynamic_axes: Dict[str, Dict[int, str]], output_idx: List[int]
    ):
        # Depth first traversal to traverse through the entire output collecting output names and dynamic axes

        if output is None:
            return
        elif isinstance(output, torch.Tensor):
            # Naming the outputs with a hyphen ensures that there can be no input with the same
            # name, preventing collisions with other NodeArgs (for example an input to forward called output0)
            output_name = f"output-{output_idx[0]}"
            output_idx[0] += 1
            output_names.append(output_name)
            output_dynamic_axes[output_name] = {}
            for dim_idx in range(len(output.shape)):
                output_dynamic_axes[output_name].update({dim_idx: f"{output_name}_dim{dim_idx}"})
            return

        if isinstance(output, abc.Sequence):
            for value in output:
                _populate_output_names_and_dynamic_axes(value, output_names, output_dynamic_axes, output_idx)
        elif isinstance(output, abc.Mapping):
            for _, value in sorted(output.items()):
                _populate_output_names_and_dynamic_axes(value, output_names, output_dynamic_axes, output_idx)
        else:
            raise wrap_exception(
                ORTModuleIOError,
                TypeError(f"ORTModule does not support the following model output type {type(output)}"),
            )

    output_names: List[str] = []
    output_dynamic_axes: Dict[str, Dict[int, str]] = {}
    output_idx: List[int] = [0]
    _populate_output_names_and_dynamic_axes(module_output, output_names, output_dynamic_axes, output_idx)

    return output_names, output_dynamic_axes


def _transform_output_to_flat_tuple(data):
    """Converts the data to a flat tuple by iterating over the entire data structure"""

    def _flatten_data(data, flat_data):
        # Recursively traverse over the data and populate the flat_data with torch.Tensors

        if data is None:
            return
        elif isinstance(data, torch.Tensor):
            identity = _OutputIdentityOp.apply
            flat_data.append(identity(data))
        elif isinstance(data, abc.Sequence):
            for value in data:
                _flatten_data(value, flat_data)
        elif isinstance(data, abc.Mapping):
            for _, value in sorted(data.items()):
                _flatten_data(value, flat_data)
        else:
            raise wrap_exception(
                ORTModuleIOError, TypeError(f"ORTModule does not support the following data type {type(data)}.")
            )

    flat_data = []
    _flatten_data(data, flat_data)
    return tuple(flat_data)


class _FlattenedModule(torch.nn.Module):
    def __init__(self, original_module: torch.nn.Module):
        super().__init__()
        self._original_module: torch.nn.Module = original_module

        # Before `forward` is called, _ort_module must be assigned
        # Updated input info is needed to expand args into *args, **kwargs
        self._input_info: Optional[_InputInfo] = None

    def forward(self, *args):
        new_args, new_kwargs = self._input_info.unflatten(args)
        return _transform_output_to_flat_tuple(self._original_module(*new_args, **new_kwargs))


def parse_inputs_for_onnx_export(
    all_input_parameters: List[inspect.Parameter],
    onnx_graph: Optional[onnx.ModelProto],
    schema: _ModelInputOutputSchemaType,
    args: Sequence[_ModelInputOutputType],
    kwargs: Mapping[str, _ModelInputOutputType],
) -> _InputInfo:
    """Parses through the model inputs and returns _InputInfo.

    Loop through all input parameters, try to flatten them into a 1-D list of inputs. For nested data in the inputs,
    construct the name in hierarchical order.

    Example 1, arg is a list, kwarg is a dict:
        args = [arg1, arg2], kwargs = {"a": 4, "b": 5},
        input_names = ["arg1", "arg2",  "a", "b"].

    Example 2, arg is a list, kwarg is a dict of mixed list and scalar:
        args = [arg1, arg2], kwargs = {"a": [4, 5], "b": 6},
        input_names = ["arg1", "arg2",  "a_0", "a_1", "b"].

    Example 3, arg is a list, kwarg is a dict of mixed dict and scalar:
        args = [arg1, arg2], kwargs = {"a": {"c": 4, "d": 5}, "b": 6},
        input_names = ["arg1", "arg2",  "a_c", "a_d", "b"].

    Args:
        all_input_parameters: All inspected input parameters from the original model forward function signature.
        onnx_graph: (optional) The onnx graph.
        schema: The schema extracted from the positional arguments and keyword arguments of the model.
        args: The positional arguments of the model.
        kwargs: The keyword arguments of the model.

    """

    def _add_dynamic_shape(name, input) -> Dict[str, Dict[int, str]]:
        dynamic_axes[name] = {}
        for dim_idx in range(len(input.shape)):
            dynamic_axes[name].update({dim_idx: f"{name}_dim{dim_idx}"})
        return dynamic_axes

    def _add_input(name, input_value, onnx_graph, onnx_graph_input_names):
        """Returns number of expanded non none inputs that _add_input processed"""

        if name in input_names or input_value is None or isinstance(input_value, str):
            # Drop all None and string inputs and return 0.
            return

        if isinstance(input_value, abc.Sequence):
            # If the input is a sequence (like a list), expand the list so that
            # each element of the list is an input by itself.
            for i, val in enumerate(input_value):
                # Name each input with the index appended to the original name of the
                # argument.
                _add_input(f"{name}_{i}", val, onnx_graph, onnx_graph_input_names)

            # Return here since the list by itself is not a valid input.
            # All the elements of the list have already been added as inputs individually.
            return
        elif isinstance(input_value, abc.Mapping):
            # If the input is a mapping (like a dict), expand the dict so that
            # each element of the dict is an input by itself.
            for key, val in input_value.items():
                _add_input(f"{name}_{key}", val, onnx_graph, onnx_graph_input_names)

            # Return here since the dict by itself is not a valid input.
            # All the elements of the dict have already been added as inputs individually.
            return

        # InputInfo should contain all the names irrespective of whether they are
        # a part of the onnx graph or not.
        input_names.append(name)

        if (onnx_graph is None or name in onnx_graph_input_names) and isinstance(input_value, torch.Tensor):
            if input_value.requires_grad:
                input_names_require_grad.append(name)
            dynamic_axes.update(_add_dynamic_shape(name, input_value))
            input_shape.append(list(input_value.size()))

    # Ignore optional inputs explicitly specified as None
    # ONNX exporter may remove unused inputs
    onnx_graph_input_names: List[str] = []
    if onnx_graph is not None:
        onnx_graph_input_names = {inp.name for inp in onnx_graph.graph.input}

    input_names: List[str] = []
    dynamic_axes: Dict[str, Dict[int, str]] = {}
    input_names_require_grad: List[str] = []
    input_shape: List[List[int]] = []
    var_positional_idx = 0

    # Be noted, all_input_parameters is a list of inspect.Parameters parsed from the original module's forward method.
    # While the execution manger's forward function will map all given model inputs to *args and **kwargs, so it is
    # possible the input parameter list cannot represent the real model inputs given here (e.g., *args, **kwargs).
    # But it is still fine to use all_input_parameters to make sure all model inputs are covered.
    #
    # Here is an example caused by the mismatch between all_input_parameters and real model inputs.
    #   def foo(*args, named_kwargs, **kwargs):
    #       ... print("foo")
    # From inspection,
    #   > ('args', <_ParameterKind.VAR_POSITIONAL: 2>)
    #   > ('named_kwargs', <_ParameterKind.KEYWORD_ONLY: 3>)
    #   > ('kwargs', <_ParameterKind.VAR_KEYWORD: 4>)
    #
    # At this point, 'named_kwargs' exists in **kwargs as a result of ORTModule's forward parse all original
    # model inputs in to *args and **kwargs.
    # When we loop `all_input_parameters``, for the `named_kwargs`, we will try to handle it in KEYWORD_ONLY branch.
    # Additionally in VAR_KEYWORD branch, we will get the `named_kwargs` value again, because its name exists in the
    # `kwargs`. So _add_input avoids handling the `named_kwargs` twice, check test case `test_named_kwargs_dict_input`
    # for the details.
    for input_idx, input_parameter in enumerate(all_input_parameters):
        if input_parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            # VAR_POSITIONAL parameter carries all *args parameters from original forward method

            for args_i in range(input_idx, len(args)):
                name = f"{input_parameter.name}_{var_positional_idx}"
                var_positional_idx += 1
                inp = args[args_i]
                _add_input(name, inp, onnx_graph, onnx_graph_input_names)
        elif (
            input_parameter.kind == inspect.Parameter.POSITIONAL_ONLY
            or input_parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
            or input_parameter.kind == inspect.Parameter.KEYWORD_ONLY
        ):
            # All positional non-*args and non-**kwargs are processed here
            name = input_parameter.name
            inp = None
            input_idx += var_positional_idx  # noqa: PLW2901
            if input_idx < len(args) and args[input_idx] is not None:
                inp = args[input_idx]
            elif name in kwargs and kwargs[name] is not None:
                inp = kwargs[name]
            _add_input(name, inp, onnx_graph, onnx_graph_input_names)
        elif input_parameter.kind == inspect.Parameter.VAR_KEYWORD:
            # **kwargs is always the last argument of forward()
            for name, inp in kwargs.items():
                _add_input(name, inp, onnx_graph, onnx_graph_input_names)

    return _InputInfo(
        names=input_names,
        shape=input_shape,
        require_grad_names=input_names_require_grad,
        dynamic_axes=dynamic_axes,
        schema=schema,
        num_positionals=len(args),
    )


def parse_outputs_for_onnx_export_and_extract_schema(
    module, args: Sequence[_ModelInputOutputType], kwargs: Mapping[str, _ModelInputOutputType], log_level: LogLevel
):
    # Perform a forward call to grab outputs
    output_names = None
    output_dynamic_axes = None
    is_deepcopy = False
    with torch.no_grad(), suppress_os_stream_output(log_level=log_level):
        # Deepcopy inputs, since input values may change after model run.
        sample_args_copy, sample_kwargs_copy = deepcopy_model_input(*args, **kwargs)
        try:
            # Deepcopy model, in case model is stateful and changes after model run.
            model_copy = copy.deepcopy(module)
            is_deepcopy = True
        except Exception:
            model_copy = module
            warnings.warn(
                "This model cannot be deep copied (or pickled), "
                "which is a required step for stateful models to be properly exported to ONNX."
                " Compute will continue, but unexpected results may occur!"
            )

        sample_outputs = model_copy(*sample_args_copy, **sample_kwargs_copy)

        # Parse the output and extract the output_names and output_dynamic_axes to be used for onnx export
        output_names, output_dynamic_axes = _parse_outputs_and_extract_names_and_dynamic_axes(sample_outputs)

    output_schema = _extract_schema(sample_outputs)
    if is_deepcopy:
        del model_copy
        gc.collect()
        if torch.cuda.is_available():
            # Trigger python GC is not enough.
            # Release the memory cached by torch.
            torch.cuda.empty_cache()
    # Return output names, output dynamic axes and output schema
    return output_names, output_dynamic_axes, output_schema
