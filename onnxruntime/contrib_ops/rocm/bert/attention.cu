// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include "contrib_ops/rocm/bert/attention.h"
#include "contrib_ops/rocm/bert/attention_impl.h"
#include "contrib_ops/rocm/bert/batched_gemm_permute_pipelines.cuh"
#include "contrib_ops/rocm/bert/batched_gemm_softmax_gemm_permute_pipelines.cuh"
#include "contrib_ops/rocm/bert/transformer_common.h"
#include "core/providers/rocm/rocm_common.h"
#include "core/providers/rocm/shared_inc/fpgeneric.h"
#include "core/providers/rocm/tunable/gemm.h"

using namespace onnxruntime::rocm;
using namespace ::onnxruntime::common;
using namespace ONNX_NAMESPACE;

namespace onnxruntime {
namespace contrib {
namespace rocm {

constexpr int kPastSequenceLengthInputIndex = 6;
constexpr int kPastInputIndex = 4;
constexpr int kPresentOutputIndex = 1;

#define REGISTER_KERNEL_TYPED(T)                                               \
  ONNX_OPERATOR_TYPED_KERNEL_EX(                                               \
      Attention,                                                               \
      kMSDomain,                                                               \
      1,                                                                       \
      T,                                                                       \
      kRocmExecutionProvider,                                                  \
      (*KernelDefBuilder::Create())                                            \
          .MayInplace(kPastInputIndex, kPresentOutputIndex)                    \
          .TypeConstraint("T", DataTypeImpl::GetTensorType<T>())               \
          .InputMemoryType(OrtMemTypeCPUInput, kPastSequenceLengthInputIndex), \
      Attention<T>);

REGISTER_KERNEL_TYPED(float)
REGISTER_KERNEL_TYPED(MLFloat16)

template <typename T>
Attention<T>::Attention(const OpKernelInfo& info) : RocmKernel(info), AttentionBase(info, true) {}

template <typename T>
Status Attention<T>::ComputeInternal(OpKernelContext* context) const {
  const Tensor* input = context->Input<Tensor>(0);
  const Tensor* weights = context->Input<Tensor>(1);
  const Tensor* bias = context->Input<Tensor>(2);
  const Tensor* mask_index = context->Input<Tensor>(3);
  const Tensor* past = context->Input<Tensor>(4);
  const Tensor* relative_position_bias = context->Input<Tensor>(5);
  const Tensor* past_seq_len = context->Input<Tensor>(kPastSequenceLengthInputIndex);

  auto& device_prop = GetDeviceProp();
  RocmAttentionParameters attn;
  ORT_RETURN_IF_ERROR(CheckInputs(input->Shape(),
                                  weights->Shape(),
                                  bias->Shape(),
                                  mask_index,
                                  past,
                                  relative_position_bias,
                                  &attn,
                                  device_prop.maxThreadsPerBlock,
                                  past_seq_len));
  ORT_ENFORCE(attn.sequence_length == attn.kv_sequence_length);  // self attention
  ORT_ENFORCE(attn.qkv_format == Q_K_V_BNSH);                    // non-packed, permuted

  TensorShapeVector output_shape(3);
  output_shape[0] = static_cast<int64_t>(attn.batch_size);
  output_shape[1] = static_cast<int64_t>(attn.sequence_length);
  output_shape[2] = static_cast<int64_t>(attn.v_hidden_size);
  Tensor* output = context->Output(0, output_shape);

  std::vector<int64_t> present_dims{
      2, attn.batch_size, attn.num_heads,
      past_present_share_buffer_ ? attn.max_sequence_length : attn.total_sequence_length,
      attn.head_size};
  TensorShape present_shape(present_dims);
  Tensor* present = context->Output(kPresentOutputIndex, present_shape);

  auto stream = Stream(context);
  rocblas_handle rocblas = GetRocblasHandle(context);

  using HipT = typename ToHipType<T>::MappedType;
  using QkvProjectGeneric = GemmPermuteGenericPipeline<HipT>;
  using AttentionGeneric = GemmSoftmaxGemmPermuteGenericPipeline<HipT>;
  using AttentionTunableOp = GemmSoftmaxGemmPermuteTunableOp<HipT>;

  ORT_RETURN_IF_ERROR(ClassifyAttentionMode(
      Node().OpType(), &attn, /*qkv=*/{}, /*past=*/{past}, /*present=*/{present}));
  // TODO: support QFMT_KFMT_VFMT_NONE_NONE_2BNMH_NONE and QFMT_KFMT_VFMT_2BNMH_NONE_2BNMH_NONE
  ORT_ENFORCE(attn.mode == QFMT_KFMT_VFMT_NONE_NONE_NONE_NONE ||
              attn.mode == QFMT_KFMT_VFMT_NONE_NONE_2BNTH_NONE ||
              attn.mode == QFMT_KFMT_VFMT_2BNPH_NONE_2BNTH_NONE);

  size_t qkv_project_output_bytes = QkvProjectGeneric::GetOutputNumBytes(&attn);
  size_t shared_workspace_bytes = std::max(QkvProjectGeneric::GetWorkspaceNumBytes(&attn),
                                           AttentionGeneric::GetWorkspaceNumBytes(&attn));
  if (GetTuningContext()->IsTunableOpEnabled()) {
    shared_workspace_bytes = std::max(shared_workspace_bytes, AttentionTunableOp::GetWorkspaceNumBytes(&attn));
  }

  auto qkv_project_output = GetScratchBuffer<void>(qkv_project_output_bytes, context->GetComputeStream());
  auto workspace = GetScratchBuffer<void>(shared_workspace_bytes, context->GetComputeStream());

  GemmPermuteParams<HipT> gemm_permute_params;
  {
    auto& params = gemm_permute_params;
    params.tuning_ctx = GetTuningContext();
    params.stream = stream;
    params.handle = rocblas;
    params.attention = &attn;
    params.device_prop = &device_prop;

    params.input_buffer = reinterpret_cast<const HipT*>(input->DataRaw());
    params.weight_buffer = reinterpret_cast<const HipT*>(weights->DataRaw());
    params.bias_buffer = reinterpret_cast<const HipT*>(bias->DataRaw());
    params.out_buffer = reinterpret_cast<HipT*>(qkv_project_output.get());
    params.ones = GetConstOnes<HipT>(attn.batch_size * attn.sequence_length, stream);
    params.workspace_buffer = reinterpret_cast<HipT*>(workspace.get());
  }

  ORT_RETURN_IF_ERROR(QkvProjectGeneric::Run(&gemm_permute_params));
  auto [q_buffer, k_buffer, v_buffer] = QkvProjectGeneric::UnspliceOutputQKV(&gemm_permute_params);

  if (nullptr != present) {
    // Concat past (2xBxNxS'xH) to present (2xBxNxTxH):
    // past_k (BxNxS'xH) + k (BxNxSxH) => present_k (BxNxTxH)
    // past_v (BxNxS'xH) + v (BxNxSxH) => present_v (BxNxTxH)
    const int batches = attn.batch_size * attn.num_heads;
    const int present_size_per_batch = attn.total_sequence_length * attn.head_size;
    ORT_RETURN_IF_ERROR(
        LaunchConcatPastToPresent(Stream(context),
                                  attn.total_sequence_length,
                                  attn.sequence_length,
                                  attn.batch_size,
                                  attn.head_size,
                                  attn.num_heads,
                                  device_prop.maxThreadsPerBlock,
                                  nullptr == past ? nullptr : reinterpret_cast<const HipT*>(past->DataRaw()),
                                  k_buffer,
                                  reinterpret_cast<HipT*>(present->MutableDataRaw())));

    // update pointers to present_k and present_v.
    k_buffer = reinterpret_cast<HipT*>(present->MutableDataRaw());
    v_buffer = reinterpret_cast<HipT*>(present->MutableDataRaw()) + batches * present_size_per_batch;
  }

  // For testing, environment variable ORT_TRANSFORMER_OPTIONS=1 could enable persistent softmax
  const TransformerOptions* options = TransformerOptions::GetInstance();
  bool use_persistent_softmax = options->IsPrecisionMode() && !options->DisablePersistentSoftmax();

  GemmSoftmaxGemmPermuteParams<HipT> gemm_softmax_gemm_permute_params;
  {
    auto& params = gemm_softmax_gemm_permute_params;
    params.tuning_ctx = GetTuningContext();
    params.stream = Stream(context);
    params.handle = rocblas;
    params.attention = &attn;
    params.device_prop = &device_prop;
    // FIXME: the params.scale seems to be different from AttentionParameters::scale;
    params.scale = 1.0f / sqrt(static_cast<float>(attn.head_size));
    params.q_buffer = q_buffer;
    params.k_buffer = k_buffer;
    params.v_buffer = v_buffer;
    params.out_buffer = reinterpret_cast<HipT*>(output->MutableDataRaw());

    if (relative_position_bias != nullptr) {
      params.bias_buffer = reinterpret_cast<const HipT*>(relative_position_bias->DataRaw());
    }

    if (mask_index != nullptr) {
      params.mask_index_buffer = mask_index->Data<int>();
      params.mask_index_dims = mask_index->Shape().AsShapeVector();
    }

    params.workspace_buffer = reinterpret_cast<HipT*>(workspace.get());
  }

  if (this->GetTuningContext()->IsTunableOpEnabled() &&
      !use_persistent_softmax) {
    return AttentionTunableOp{}(&gemm_softmax_gemm_permute_params);
  } else {
    return AttentionGeneric::Run(&gemm_softmax_gemm_permute_params, use_persistent_softmax);
  }
}

}  // namespace rocm
}  // namespace contrib
}  // namespace onnxruntime
