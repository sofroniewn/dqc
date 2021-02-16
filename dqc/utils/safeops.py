import math
import torch
from typing import Union, Optional, Tuple
from dqc.utils.datastruct import ZType

eps = 1e-12

########################## safe operations ##########################

def safepow(a: torch.Tensor, p: torch.Tensor, eps: float = 1e-12) -> torch.Tensor:
    if torch.any(a < 0):
        raise RuntimeError("safepow only works for positive base")
    base = torch.sqrt(a * a + eps * eps)  # soft clip
    return base ** p

def safenorm(a: torch.Tensor, dim: int, eps: float = 1e-15) -> torch.Tensor:
    # calculate the 2-norm safely
    return torch.sqrt(torch.sum(a * a + eps * eps, dim=dim))

########################## occupation number gradients ##########################
def occnumber(a: ZType,
              n: Optional[int] = None,
              dtype: torch.dtype = torch.double,
              device: torch.device = torch.device('cpu')) -> torch.Tensor:
    # returns the occupation number (maxed at 1) where the total sum of the
    # output equals to a with length of the output is n

    def _get_floor_and_ceil(aa: Union[int, float]) -> Tuple[int, int]:
        # get the ceiling and flooring of aa
        if isinstance(aa, int):
            ceil_a: int = aa
            floor_a: int = aa
        else:  # floor
            ceil_a = int(math.ceil(aa))
            floor_a = int(math.floor(aa))
        return floor_a, ceil_a

    if isinstance(a, torch.Tensor):
        assert a.numel() == 1
        floor_a, ceil_a = _get_floor_and_ceil(a.item())
    else:  # int or float
        floor_a, ceil_a = _get_floor_and_ceil(a)

    # get the length of the tensor output
    if n is None:
        nlength = ceil_a
    else:
        nlength = n
        assert nlength >= ceil_a, "The length of occupation number must be at least %d" % ceil_a

    if isinstance(a, torch.Tensor):
        res = _OccNumber.apply(a, floor_a, ceil_a, nlength, dtype, device)
    else:
        res = _construct_occ_number(a, floor_a, ceil_a, nlength, dtype=dtype, device=device)
    return res

def _construct_occ_number(a: float, floor_a: int, ceil_a: int, nlength: int,
                          dtype: torch.dtype, device: torch.device) -> torch.Tensor:
    res = torch.zeros(nlength, dtype=dtype, device=device)
    res[:floor_a] = 1
    if ceil_a > floor_a:
        res[ceil_a - 1] = a - floor_a
    return res

class _OccNumber(torch.autograd.Function):
    @staticmethod
    def forward(ctx, a: torch.Tensor,  # type: ignore
                floor_a: int, ceil_a: int, nlength: int,
                dtype: torch.dtype, device: torch.device) -> torch.Tensor:
        res = _construct_occ_number(float(a.item()), floor_a, ceil_a, nlength, dtype=dtype, device=device)
        ctx.ceil_a = ceil_a
        return res

    @staticmethod
    def backward(ctx, grad_res: torch.Tensor):  # type: ignore
        grad_a = grad_res[ctx.ceil_a - 1]
        return (grad_a,) + (None,) * 5
