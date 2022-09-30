from typing import Optional

import torch
import torch.nn as nn
from torch import Tensor
from torch_geometric.typing import OptTensor, Adj

from tsl.nn.functional import reverse_tensor
from tsl.nn.layers.graph_convs.grin_cell import GRIL
from ..base_model import BaseModel
from ...base.embedding import StaticGraphEmbedding


class GRINModel(BaseModel):
    r"""The Graph Recurrent Imputation Network with DCRNN cells from the paper
    `"Filling the G_ap_s: Multivariate Time Series Imputation by Graph Neural
    Networks" <https://arxiv.org/abs/2108.00298>`_ (Cini et al., ICLR 2022).

    Args:
        input_size (int): Size of the input.
        hidden_size (int): Number of units in the DCRNN hidden layer.
        ff_size (int): Number of units in the nonlinear readout.
        embedding_size (int, optional): Number of features in the optional node
            embeddings.
        exog_size (int): Number of channels for the exogenous variables.
        n_layers (int): Number DCRNN cells.
        n_nodes (int, optional): Number of nodes in the input graph.
        kernel_size (int): Order of the spatial diffusion process.
        layer_norm (bool, optional): Whether to use layer normalization
        dropout (float, optional): Dropout probability in the DCRNN cells.
        ff_dropout (float, optional): Dropout probability in the readout.
        merge_mode (str, optional): Strategy used to merge representations
            coming from the two branches of the bidirectional model.
            (default: :obj:`mlp`)
    """

    def __init__(self, input_size: int,
                 hidden_size: int = 64,
                 ff_size: int = 128,
                 embedding_size: Optional[int] = None,
                 exog_size: Optional[int] = None,
                 n_layers: int = 1,
                 n_nodes: Optional[int] = None,
                 kernel_size: int = 2,
                 decoder_order: int = 1,
                 layer_norm: bool = False,
                 dropout: float = 0.,
                 ff_dropout: float = 0.,
                 merge_mode: str = 'mlp'):
        super(GRINModel, self).__init__()
        self.fwd_gril = GRIL(input_size=input_size,
                             hidden_size=hidden_size,
                             exog_size=exog_size,
                             n_layers=n_layers,
                             dropout=dropout,
                             kernel_size=kernel_size,
                             decoder_order=decoder_order,
                             n_nodes=n_nodes,
                             layer_norm=layer_norm)
        self.bwd_gril = GRIL(input_size=input_size,
                             hidden_size=hidden_size,
                             exog_size=exog_size,
                             n_layers=n_layers,
                             dropout=dropout,
                             kernel_size=kernel_size,
                             decoder_order=decoder_order,
                             n_nodes=n_nodes,
                             layer_norm=layer_norm)

        if embedding_size is not None:
            assert n_nodes is not None
            self.emb = StaticGraphEmbedding(n_nodes, embedding_size)
        else:
            self.register_parameter('emb', None)

        self.merge_mode = merge_mode
        if merge_mode == 'mlp':
            in_channels = 4 * hidden_size + input_size + embedding_size
            self.out = nn.Sequential(nn.Linear(in_channels, ff_size),
                                     nn.ReLU(),
                                     nn.Dropout(ff_dropout),
                                     nn.Linear(ff_size, input_size))
        elif merge_mode in ['mean', 'sum', 'min', 'max']:
            self.out = getattr(torch, merge_mode)
        else:
            raise ValueError("Merge option %s not allowed." % merge_mode)

    def forward(self, x: Tensor, edge_index: Adj,
                edge_weight: OptTensor = None, mask: OptTensor = None,
                u: OptTensor = None):
        # x: [batch, steps, nodes, channels]
        fwd_out, fwd_pred, fwd_repr, _ = self.fwd_gril(x,
                                                       edge_index, edge_weight,
                                                       mask=mask, u=u)
        # Backward
        rev_x = reverse_tensor(x, dim=1)
        rev_mask = reverse_tensor(mask, dim=1) if mask is not None else None
        rev_u = reverse_tensor(u, dim=1) if u is not None else None
        *bwd, _ = self.bwd_gril(rev_x, edge_index, edge_weight,
                                mask=rev_mask, u=rev_u)
        bwd_out, bwd_pred, bwd_repr = [reverse_tensor(res, 1) for res in bwd]

        if self.merge_mode == 'mlp':
            inputs = [fwd_repr, bwd_repr, mask]
            if self.emb is not None:
                b, s, *_ = fwd_repr.size()  # fwd_h: [b t n f]
                inputs += [self.emb(expand=(b, s, -1, -1))]
            imputation = torch.cat(inputs, dim=-1)
            imputation = self.out(imputation)
        else:
            imputation = torch.stack([fwd_out, bwd_out], dim=-1)
            imputation = self.out(imputation, dim=-1)

        return imputation, (fwd_out, bwd_out, fwd_pred, bwd_pred)
