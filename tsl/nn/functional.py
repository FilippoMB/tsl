import math
from typing import Optional

import torch
import torch.nn.functional as F
from torch import Tensor
from torch_geometric.utils.num_nodes import maybe_num_nodes
from torch_scatter import scatter
from torch_scatter.utils import broadcast

import tsl

__all__ = [
    'gated_tanh',
    'reverse_tensor',
    'sparse_softmax',
    'sparse_multi_head_attention'
]


@torch.jit.script
def gated_tanh(input: Tensor, dim: int = -1) -> Tensor:
    r"""The gated tanh unite. Computes:

    .. math ::
        \text{GatedTanH}(a, b) = \text{TanH}(a) \otimes \sigma(b)

    where `input` is split in half along `dim` to form `a` and `b`, :math:`\text{TanH}` is the hyperbolic tangent
    function, :math:`\sigma` is the sigmoid function and :math:`\otimes` is the element-wise product between matrices.

    Args:
        input (Tensor): Input tensor.
        dim (int, optional): Dimension on which to split the input.
                             (default: -1)
    """

    out, gate = torch.tensor_split(input, 2, dim=dim)
    return torch.tanh(out) * torch.sigmoid(gate)


@torch.jit.script
def reverse_tensor(tensor: Tensor, dim: int) -> Tensor:
    """Reverse tensor along specific dimension.

    Args:
        tensor (Tensor): Input tensor.
        dim (int): Dimension along which to reverse sequence.
    """
    indices = torch.arange(tensor.size(dim) - 1, -1, -1, device=tensor.device)
    return tensor.index_select(dim, indices)


@torch.jit.script
def sparse_softmax(src: Tensor, index: Tensor, num_nodes: Optional[int] = None,
                   dim: int = -2) -> Tensor:
    r"""Extension of ~torch_geometric.softmax with index broadcasting to compute
    a sparsely evaluated softmax.

    Given a value tensor :attr:`src`, this function first groups the values
    along the first dimension based on the indices specified in :attr:`index`,
    and then proceeds to compute the softmax individually for each group.

    Args:
        src (Tensor): The source tensor.
        index (Tensor): The indices of elements for applying the softmax.
        num_nodes (int, optional): The number of nodes, *i.e.*
            :obj:`max_val + 1` of :attr:`index`. (default: :obj:`None`)
        dim (int, optional): The dimension in which to normalize, i.e., the edge
            dimension. (default: :obj:`-2`)
    """
    N = maybe_num_nodes(index, num_nodes)
    expanded_index = broadcast(index, src, dim)
    src_max = scatter(src, expanded_index, dim, dim_size=N, reduce='max')
    src_max = src_max.index_select(dim, index)
    out = (src - src_max).exp()
    out_sum = scatter(out, expanded_index, dim, dim_size=N, reduce='sum')
    out_sum = out_sum.index_select(dim, index)

    return out / (out_sum + tsl.epsilon)


@torch.jit.script
def sparse_multi_head_attention(q: Tensor, k: Tensor, v: Tensor, index: Tensor,
                                dim_size: Optional[int] = None,
                                dropout_p: float = 0.0):
    r"""Computes multi-head, scaled, dot product attention on query, key and
    value tensors, applying dropout if a probability greater than 0.0 is
    specified. Index specifies for each query in q the belonging sequence in the
    original batched, dense tensor.
    Returns a tensor pair containing attended values and attention weights.

    Args:
        q (Tensor): Query tensor. See Shape section for shape details.
        k (Tensor): Key tensor. See Shape section for shape details.
        v (Tensor): Value tensor. See Shape section for shape details.
        index (Tensor): Tensor containing mask values to be added to calculated
            attention. May be 2D or 3D; see Shape section for details.
        dim_size (int, optional): The batched target length sequence, i.e.
            :obj:`max_val + 1` of :attr:`index`. (default: :obj:`None`)
        dropout_p: dropout probability. If greater than 0.0, dropout is applied.

    Shape:
        - q: :math:`(S, H, E)` where S is sparsed dimension, H is the number of
            heads, and E is embedding dimension.
        - k: :math:`(S, H, E)` where S is sparsed dimension, H is the number of
            heads, and E is embedding dimension.
        - v: :math:`(S, H, O)` where S is sparsed dimension, H is the number of
            heads, and O is output dimension.
        - index: :math:`(S)` where S is sparsed dimension.
        - dim_size: must be :math:`(B \times Nt)`

        - Output: attention values have shape :math:`(B, Nt, E)`; attention
            weights have shape :math:`(S, H)`
    """
    dim = 0
    B, H, E = q.shape
    N = maybe_num_nodes(index, dim_size)
    # scores
    alpha = (q * k).sum(dim=-1) / math.sqrt(E)
    alpha = sparse_softmax(alpha, index, N, dim)
    if dropout_p > 0.0:
        alpha = F.dropout(alpha, p=dropout_p)
    v *= alpha.view(-1, H, 1)
    # out
    out = torch.zeros((N, H, v.size(2)), dtype=v.dtype, device=v.device)
    add_index = broadcast(index, v, dim)
    out.scatter_add_(dim, add_index, v)
    return out, alpha