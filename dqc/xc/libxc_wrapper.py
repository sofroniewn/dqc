from __future__ import annotations
from typing import Mapping, Tuple, Optional, Union, Iterator, List
import torch
import numpy as np
import warnings
try:
    import pylibxc
except (ImportError, ModuleNotFoundError) as e:
    warnings.warn("Failed to import pylibxc. Might not be able to use xc.")

############################ libxc with derivative ############################

# This is the interface of libxc to pytorch to make the it differentiable
# in pytorch format.
# The torch inputs are flattened and should have been checked to have the
# same length and shape, i.e. (ninps).

class CalcLDALibXCUnpol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho: torch.Tensor, deriv: int,  # type: ignore
                libxcfcn: pylibxc.functional.LibXCFunctional) -> \
            Tuple[torch.Tensor, ...]:  # type: ignore
        # Calculates and returns the energy density or its derivative w.r.t.
        # density.
        # The result is a tensor with shape (ninps)

        inp = {
            "rho": rho.detach().numpy(),
        }
        res = _get_libxc_res(inp, deriv, libxcfcn, family=1, polarized=False)[0]

        ctx.save_for_backward(rho, res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (res,)

    @staticmethod
    def backward(ctx, *grad_res: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        rho, res = ctx.saved_tensors

        dres_drho = CalcLDALibXCUnpol.apply(rho, ctx.deriv + 1, ctx.libxcfcn)[0]
        grad_rho = dres_drho * grad_res[0]
        return (grad_rho, None, None)

class CalcLDALibXCPol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho_u: torch.Tensor, rho_d: torch.Tensor, deriv: int,  # type: ignore
                libxcfcn: pylibxc.functional.LibXCFunctional) -> Tuple[torch.Tensor, ...]:
        # Calculates and returns the energy density or its derivative w.r.t.
        # density.
        # The result is a tensor with shape (nderiv, ninps) where the first
        # dimension indicates the result for derivatives of spin-up and
        # spin-down and some of its combination.

        inp = {
            "rho": _pack_input(rho_u, rho_d),
        }
        res = _get_libxc_res(inp, deriv, libxcfcn, family=1, polarized=True)[0]

        ctx.save_for_backward(rho_u, rho_d, res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (res,)

    @staticmethod
    def backward(ctx,  # type: ignore
                 *grad_res: torch.Tensor) -> \
            Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        inps = ctx.saved_tensors[:2]
        res = ctx.saved_tensors[2:]
        deriv = ctx.deriv
        libxcfcn = ctx.libxcfcn

        derivs = CalcLDALibXCPol.apply(*inps, deriv + 1, libxcfcn)

        # generated by `_generate_spin_list(deriv, ["rho"], [2])`
        if deriv == 0:
            deriv_idxs = [[0], [0]]
            spin_idxs: List[List[Tuple[int, ...]]] = [[(0,)], [(1,)]]
        elif deriv == 1:
            deriv_idxs = [[0], [0]]
            spin_idxs = [[(0, 1)], [(1, 2)]]
        elif deriv == 2:
            deriv_idxs = [[0], [0]]
            spin_idxs = [[(0, 1, 2)], [(1, 2, 3)]]
        elif deriv == 3:
            deriv_idxs = [[0], [0]]
            spin_idxs = [[(0, 1, 2, 3)], [(1, 2, 3, 4)]]
        else:
            raise RuntimeError(f"Unimplemented derivative for deriv == {deriv} for polarized LDA")

        grad_inps = _get_grad_inps(grad_res, inps, derivs, ctx.needs_input_grad,
                                   deriv_idxs, spin_idxs)
        return (*grad_inps, None, None)

class CalcGGALibXCUnpol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho: torch.Tensor, sigma: torch.Tensor, deriv: int,  # type: ignore
                libxcfcn: pylibxc.functional.LibXCFunctional) ->\
            Tuple[torch.Tensor, ...]:  # type: ignore
        # Calculates and returns the energy density or its derivative w.r.t.
        # density and contracted gradient.
        # Every element in the tuple is a tensor with shape (ninps)

        inp = {
            "rho": rho,
            "sigma": sigma,
        }
        # for gga, res is a tuple
        res = _get_libxc_res(inp, deriv, libxcfcn, family=2, polarized=False)

        ctx.save_for_backward(rho, sigma, *res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (*res,)

    @staticmethod
    def backward(ctx, *grad_res: torch.Tensor) -> \
            Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        inps = ctx.saved_tensors[:2]
        res = ctx.saved_tensors[2:]
        deriv = ctx.deriv
        libxcfcn = ctx.libxcfcn

        derivs = CalcGGALibXCUnpol.apply(*inps, deriv + 1, libxcfcn)

        # generated by _generate_pair_deriv_idxs(deriv, ["rho", "sigma"])
        # see _get_grad_inps for explanation about deriv_idxs
        if deriv == 0:
            deriv_idxs = [[0], [1]]
        elif deriv == 1:
            deriv_idxs = [[0, 1], [1, 2]]
        elif deriv == 2:
            deriv_idxs = [[0, 1, 2], [1, 2, 3]]
        elif deriv == 3:
            deriv_idxs = [[0, 1, 2, 3], [1, 2, 3, 4]]
        else:
            raise RuntimeError("Cannot handle GGA deriv %d" % deriv)

        grad_inps = _get_grad_inps(grad_res, inps, derivs, ctx.needs_input_grad, deriv_idxs)
        return (*grad_inps, None, None)

class CalcGGALibXCPol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho_u: torch.Tensor, rho_d: torch.Tensor,  # type: ignore
                sigma_uu: torch.Tensor, sigma_ud: torch.Tensor, sigma_dd: torch.Tensor,
                deriv: int, libxcfcn: pylibxc.functional.LibXCFunctional) -> \
            Tuple[torch.Tensor, ...]:  # type: ignore
        # Calculates and returns the energy density or its derivative w.r.t.
        # density and contracted gradient.
        # Every element in the tuple is a tensor with shape of (nderiv, ninps)
        # where nderiv depends on the number of derivatives for spin-up and
        # spin-down combinations, e.g. nderiv == 3 for vsigma (see libxc manual)

        inp = {
            "rho": _pack_input(rho_u, rho_d),
            "sigma": _pack_input(sigma_uu, sigma_ud, sigma_dd),
        }
        res = _get_libxc_res(inp, deriv, libxcfcn, family=2, polarized=True)

        ctx.save_for_backward(rho_u, rho_d, sigma_uu, sigma_ud, sigma_dd, *res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (*res,)

    @staticmethod
    def backward(ctx, *grad_res: torch.Tensor) -> \
            Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        inps = ctx.saved_tensors[:5]
        res = ctx.saved_tensors[5:]
        deriv = ctx.deriv
        libxcfcn = ctx.libxcfcn

        derivs = CalcGGALibXCPol.apply(*inps, deriv + 1, libxcfcn)

        # generated by `_generate_spin_list(deriv, ["rho", "sigma"], [2, 3])`
        if deriv == 0:
            deriv_idxs = [[0], [0], [1], [1], [1]]
            spin_idxs: List[List[Tuple[int, ...]]] = [[(0,)],
                                                      [(1,)],
                                                      [(0,)],
                                                      [(1,)],
                                                      [(2,)]]
        elif deriv == 1:
            deriv_idxs = [[0, 1], [0, 1], [1, 2], [1, 2], [1, 2]]
            spin_idxs = [[(0, 1), (0, 1, 2)],
                         [(1, 2), (3, 4, 5)],
                         [(0, 3), (0, 1, 2)],
                         [(1, 4), (1, 3, 4)],
                         [(2, 5), (2, 4, 5)]]
        elif deriv == 2:
            deriv_idxs = [[0, 1, 2], [0, 1, 2], [1, 2, 3], [1, 2, 3], [1, 2, 3]]
            spin_idxs = [[(0, 1, 2), (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5)],
                         [(1, 2, 3), (3, 4, 5, 6, 7, 8), (6, 7, 8, 9, 10, 11)],
                         [(0, 3, 6), (0, 1, 2, 6, 7, 8), (0, 1, 2, 3, 4, 5)],
                         [(1, 4, 7), (1, 3, 4, 7, 9, 10), (1, 3, 4, 6, 7, 8)],
                         [(2, 5, 8), (2, 4, 5, 8, 10, 11), (2, 4, 5, 7, 8, 9)]]
        elif deriv == 3:
            deriv_idxs = [[0, 1, 2, 3], [0, 1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4]]
            spin_idxs = [[(0, 1, 2, 3), (0, 1, 2, 3, 4, 5, 6, 7, 8),
                          (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)],
                         [(1, 2, 3, 4), (3, 4, 5, 6, 7, 8, 9, 10, 11),
                          (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17),
                          (10, 11, 12, 13, 14, 15, 16, 17, 18, 19)],
                         [(0, 3, 6, 9), (0, 1, 2, 6, 7, 8, 12, 13, 14),
                          (0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15),
                          (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)],
                         [(1, 4, 7, 10), (1, 3, 4, 7, 9, 10, 13, 15, 16),
                          (1, 3, 4, 6, 7, 8, 11, 13, 14, 16, 17, 18),
                          (1, 3, 4, 6, 7, 8, 10, 11, 12, 13)],
                         [(2, 5, 8, 11), (2, 4, 5, 8, 10, 11, 14, 16, 17),
                          (2, 4, 5, 7, 8, 9, 12, 14, 15, 17, 18, 19),
                          (2, 4, 5, 7, 8, 9, 11, 12, 13, 14)]]
        else:
            raise RuntimeError(f"Unimplemented derivative for deriv == {deriv} for polarized GGA")

        grad_inps = _get_grad_inps(grad_res, inps, derivs, ctx.needs_input_grad,
                                   deriv_idxs, spin_idxs)
        return (*grad_inps, None, None)

class CalcMGGALibXCUnpol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho: torch.Tensor, sigma: torch.Tensor, lapl: torch.Tensor,  # type: ignore
                kin: torch.Tensor, deriv: int,
                libxcfcn: pylibxc.functional.LibXCFunctional) ->\
            Tuple[torch.Tensor, ...]:  # type: ignore
        # Calculates and returns the energy density or its derivative w.r.t.
        # density and contracted gradient.
        # Every element in the tuple is a tensor with shape (ninps)

        inp = {
            "rho": rho,
            "sigma": sigma,
            "lapl": lapl,
            "tau": kin,
        }
        # res is a tuple
        res = _get_libxc_res(inp, deriv, libxcfcn, family=4, polarized=False)

        ctx.save_for_backward(rho, sigma, lapl, kin, *res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (*res,)

    @staticmethod
    def backward(ctx, *grad_res: torch.Tensor) -> \
            Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        inps = ctx.saved_tensors[:4]
        res = ctx.saved_tensors[4:]
        deriv = ctx.deriv
        libxcfcn = ctx.libxcfcn

        derivs = CalcMGGALibXCUnpol.apply(*inps, deriv + 1, libxcfcn)

        # generated by _generate_pair_deriv_idxs(deriv, ["rho", "sigma", "lapl", "tau"])
        # see _get_grad_inps for explanation about deriv_idxs
        if deriv == 0:
            deriv_idxs = [[0], [1], [2], [3]]
        elif deriv == 1:
            deriv_idxs = [[0, 1, 2, 3], [1, 4, 5, 6], [2, 5, 7, 8], [3, 6, 8, 9]]
        elif deriv == 2:
            deriv_idxs = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                          [1, 4, 5, 6, 10, 11, 12, 13, 14, 15],
                          [2, 5, 7, 8, 11, 13, 14, 16, 17, 18],
                          [3, 6, 8, 9, 12, 14, 15, 17, 18, 19]]
        elif deriv == 3:
            deriv_idxs = [
                [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
                [1, 4, 5, 6, 10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29],
                [2, 5, 7, 8, 11, 13, 14, 16, 17, 18, 21, 23, 24, 26, 27, 28, 30, 31, 32, 33],
                [3, 6, 8, 9, 12, 14, 15, 17, 18, 19, 22, 24, 25, 27, 28, 29, 31, 32, 33, 34]
            ]
        else:
            raise RuntimeError("Cannot handle MGGA deriv %d" % deriv)

        grad_inps = _get_grad_inps(grad_res, inps, derivs, ctx.needs_input_grad, deriv_idxs)
        return (*grad_inps, None, None)

class CalcMGGALibXCPol(torch.autograd.Function):
    @staticmethod
    def forward(ctx, rho_u: torch.Tensor, rho_d: torch.Tensor,  # type: ignore
                sigma_uu: torch.Tensor, sigma_ud: torch.Tensor, sigma_dd: torch.Tensor,
                lapl_u: torch.Tensor, lapl_d: torch.Tensor,
                kin_u: torch.Tensor, kin_d: torch.Tensor,
                deriv: int, libxcfcn: pylibxc.functional.LibXCFunctional) -> \
            Tuple[torch.Tensor, ...]:  # type: ignore
        # Calculates and returns the energy density or its derivative w.r.t.
        # density and contracted gradient and laplacian and kinetic energy density.
        # Every element in the tuple is a tensor with shape of (nderiv, ninps)
        # where nderiv depends on the number of derivatives for spin-up and
        # spin-down combinations, e.g. nderiv == 3 for vsigma (see libxc manual)

        inp = {
            "rho": _pack_input(rho_u, rho_d),
            "sigma": _pack_input(sigma_uu, sigma_ud, sigma_dd),
            "lapl": _pack_input(lapl_u, lapl_d),
            "tau": _pack_input(kin_u, kin_d),
        }
        res = _get_libxc_res(inp, deriv, libxcfcn, family=4, polarized=True)

        ctx.save_for_backward(rho_u, rho_d, sigma_uu, sigma_ud, sigma_dd,
                              lapl_u, lapl_d, kin_u, kin_d, *res)
        ctx.deriv = deriv
        ctx.libxcfcn = libxcfcn
        return (*res,)

    @staticmethod
    def backward(ctx, *grad_res: torch.Tensor) -> \
            Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        inps = ctx.saved_tensors[:9]
        res = ctx.saved_tensors[9:]
        deriv = ctx.deriv
        libxcfcn = ctx.libxcfcn

        derivs = CalcMGGALibXCPol.apply(*inps, deriv + 1, libxcfcn)

        # generated by `_generate_spin_list(deriv, ["rho", "sigma"], [2, 3])`
        if deriv == 0:
            deriv_idxs = [[0], [0], [1], [1], [1], [2], [2], [3], [3]]
            spin_idxs: List[List[Tuple[int, ...]]] = [
                [(0,)], [(1,)], [(0,)], [(1,)], [(2,)], [(0,)], [(1,)], [(0,)], [(1,)]
            ]
        elif deriv == 1:
            deriv_idxs = [[0, 1, 2, 3], [0, 1, 2, 3], [1, 4, 5, 6], [1, 4, 5, 6],
                          [1, 4, 5, 6], [2, 5, 7, 8], [2, 5, 7, 8], [3, 6, 8, 9],
                          [3, 6, 8, 9]]
            spin_idxs = [[(0, 1), (0, 1, 2), (0, 1), (0, 1)],
                         [(1, 2), (3, 4, 5), (2, 3), (2, 3)],
                         [(0, 3), (0, 1, 2), (0, 1), (0, 1)],
                         [(1, 4), (1, 3, 4), (2, 3), (2, 3)],
                         [(2, 5), (2, 4, 5), (4, 5), (4, 5)],
                         [(0, 2), (0, 2, 4), (0, 1), (0, 1)],
                         [(1, 3), (1, 3, 5), (1, 2), (2, 3)],
                         [(0, 2), (0, 2, 4), (0, 2), (0, 1)],
                         [(1, 3), (1, 3, 5), (1, 3), (1, 2)]]
        elif deriv == 2:
            deriv_idxs = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                          [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                          [1, 4, 5, 6, 10, 11, 12, 13, 14, 15],
                          [1, 4, 5, 6, 10, 11, 12, 13, 14, 15],
                          [1, 4, 5, 6, 10, 11, 12, 13, 14, 15],
                          [2, 5, 7, 8, 11, 13, 14, 16, 17, 18],
                          [2, 5, 7, 8, 11, 13, 14, 16, 17, 18],
                          [3, 6, 8, 9, 12, 14, 15, 17, 18, 19],
                          [3, 6, 8, 9, 12, 14, 15, 17, 18, 19]]
            spin_idxs = [[(0, 1, 2), (0, 1, 2, 3, 4, 5), (0, 1, 2, 3), (0, 1, 2, 3),
                          (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5),
                          (0, 1, 2), (0, 1, 2, 3), (0, 1, 2)],
                         [(1, 2, 3), (3, 4, 5, 6, 7, 8), (2, 3, 4, 5), (2, 3, 4, 5),
                          (6, 7, 8, 9, 10, 11), (6, 7, 8, 9, 10, 11), (6, 7, 8, 9, 10, 11),
                          (3, 4, 5), (4, 5, 6, 7), (3, 4, 5)],
                         [(0, 3, 6), (0, 1, 2, 6, 7, 8), (0, 1, 6, 7), (0, 1, 6, 7),
                          (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5),
                          (0, 1, 2), (0, 1, 2, 3), (0, 1, 2)],
                         [(1, 4, 7), (1, 3, 4, 7, 9, 10), (2, 3, 8, 9), (2, 3, 8, 9),
                          (1, 3, 4, 6, 7, 8), (2, 3, 6, 7, 8, 9), (2, 3, 6, 7, 8, 9),
                          (3, 4, 5), (4, 5, 6, 7), (3, 4, 5)],
                         [(2, 5, 8), (2, 4, 5, 8, 10, 11), (4, 5, 10, 11), (4, 5, 10, 11),
                          (2, 4, 5, 7, 8, 9), (4, 5, 8, 9, 10, 11), (4, 5, 8, 9, 10, 11),
                          (6, 7, 8), (8, 9, 10, 11), (6, 7, 8)],
                         [(0, 2, 4), (0, 2, 4, 6, 8, 10), (0, 1, 3, 4), (0, 1, 4, 5),
                          (0, 2, 4, 6, 8, 10), (0, 1, 3, 4, 6, 7), (0, 1, 4, 5, 8, 9),
                          (0, 1, 2), (0, 1, 2, 3), (0, 1, 2)],
                         [(1, 3, 5), (1, 3, 5, 7, 9, 11), (1, 2, 4, 5), (2, 3, 6, 7),
                          (1, 3, 5, 7, 9, 11), (1, 2, 4, 5, 7, 8), (2, 3, 6, 7, 10, 11),
                          (1, 2, 3), (2, 3, 4, 5), (3, 4, 5)],
                         [(0, 2, 4), (0, 2, 4, 6, 8, 10), (0, 2, 4, 6), (0, 1, 3, 4),
                          (0, 2, 4, 6, 8, 10), (0, 2, 4, 6, 8, 10), (0, 1, 3, 4, 6, 7),
                          (0, 2, 4), (0, 1, 3, 4), (0, 1, 2)],
                         [(1, 3, 5), (1, 3, 5, 7, 9, 11), (1, 3, 5, 7), (1, 2, 4, 5),
                          (1, 3, 5, 7, 9, 11), (1, 3, 5, 7, 9, 11), (1, 2, 4, 5, 7, 8),
                          (1, 3, 5), (1, 2, 4, 5), (1, 2, 3)]]
        else:
            raise RuntimeError(f"Unimplemented derivative for deriv == {deriv} for polarized MGGA")

        grad_inps = _get_grad_inps(grad_res, inps, derivs, ctx.needs_input_grad,
                                   deriv_idxs, spin_idxs)
        return (*grad_inps, None, None)

def _get_libxc_res(inp: Mapping[str, Union[np.ndarray, Tuple[np.ndarray, ...], torch.Tensor, Tuple[torch.Tensor, ...]]],
                   deriv: int,
                   libxcfcn: pylibxc.functional.LibXCFunctional,
                   family: int, polarized: bool) -> Tuple[torch.Tensor, ...]:
    # deriv == 0 for energy per unit volume
    # deriv == 1 for vrho (1st derivative of energy/volume w.r.t. density)
    # deriv == 2 for v2rho2
    # deriv == 3 for v3rho3
    # deriv == 4 for v4rho4
    do_exc, do_vxc, do_fxc, do_kxc, do_lxc = _get_dos(deriv)

    res = libxcfcn.compute(
        inp,
        do_exc=do_exc, do_vxc=do_vxc, do_fxc=do_fxc,
        do_kxc=do_kxc, do_lxc=do_lxc
    )

    # compile the results in a tuple with order given in the *_KEYS (e.g. LDA_KEYS)
    res = _extract_returns(res, deriv, family)

    # In libxc, "zk" is the only one returning the energy density
    # per unit volume PER UNIT PARTICLE.
    # everything else is represented by the energy density per unit volume
    # only.
    if deriv == 0:
        rho = inp["rho"]
        if polarized:
            assert isinstance(rho, np.ndarray)
            start = np.zeros(1, dtype=rho.dtype)
            rho = sum(_unpack_input(rho), start)  # rho[:, 0] + rho[:, 1]
        res0 = res[0] * rho
        res = (res0, *res[1:])

    return res

def _pack_input(*vals: torch.Tensor) -> np.ndarray:
    # arrange the values in a numpy array with fortran memory order
    vals_np = np.asarray([val.detach().numpy() for val in vals])
    return np.ascontiguousarray(vals_np.T)

def _unpack_input(inp: np.ndarray) -> Iterator[np.ndarray]:
    # unpack from libxc input format into tuple of inputs
    return (a for a in inp.T)

def _get_dos(deriv: int) -> Tuple[bool, ...]:
    do_exc = deriv == 0
    do_vxc = deriv == 1
    do_fxc = deriv == 2
    do_kxc = deriv == 3
    do_lxc = deriv == 4
    return do_exc, do_vxc, do_fxc, do_kxc, do_lxc

# generated by [_generate_keys(i, ["rho", "sigma"]) for i in range(5)]
# _generate_keys function is below
LDA_KEYS = [["zk"], ["vrho"], ["v2rho2"], ["v3rho3"], ["v4rho4"]]
GGA_KEYS = [["zk"],
            ["vrho", "vsigma"],
            ["v2rho2", "v2rhosigma", "v2sigma2"],
            ["v3rho3", "v3rho2sigma", "v3rhosigma2", "v3sigma3"],
            ["v4rho4", "v4rho3sigma", "v4rho2sigma2", "v4rhosigma3", "v4sigma4"]]
MGGA_KEYS = [['zk'],
             ['vrho', 'vsigma', 'vlapl', 'vtau'],
             ['v2rho2', 'v2rhosigma', 'v2rholapl', 'v2rhotau', 'v2sigma2',
              'v2sigmalapl', 'v2sigmatau', 'v2lapl2', 'v2lapltau', 'v2tau2'],
             ['v3rho3', 'v3rho2sigma', 'v3rho2lapl', 'v3rho2tau', 'v3rhosigma2',
              'v3rhosigmalapl', 'v3rhosigmatau', 'v3rholapl2', 'v3rholapltau',
              'v3rhotau2', 'v3sigma3', 'v3sigma2lapl', 'v3sigma2tau', 'v3sigmalapl2',
              'v3sigmalapltau', 'v3sigmatau2', 'v3lapl3', 'v3lapl2tau', 'v3lapltau2',
              'v3tau3'],
             ['v4rho4', 'v4rho3sigma', 'v4rho3lapl', 'v4rho3tau', 'v4rho2sigma2',
              'v4rho2sigmalapl', 'v4rho2sigmatau', 'v4rho2lapl2', 'v4rho2lapltau',
              'v4rho2tau2', 'v4rhosigma3', 'v4rhosigma2lapl', 'v4rhosigma2tau',
              'v4rhosigmalapl2', 'v4rhosigmalapltau', 'v4rhosigmatau2', 'v4rholapl3',
              'v4rholapl2tau', 'v4rholapltau2', 'v4rhotau3', 'v4sigma4', 'v4sigma3lapl',
              'v4sigma3tau', 'v4sigma2lapl2', 'v4sigma2lapltau', 'v4sigma2tau2',
              'v4sigmalapl3', 'v4sigmalapl2tau', 'v4sigmalapltau2', 'v4sigmatau3',
              'v4lapl4', 'v4lapl3tau', 'v4lapl2tau2', 'v4lapltau3', 'v4tau4']]

def _extract_returns(ret: Mapping[str, np.ndarray], deriv: int, family: int) -> \
        Tuple[torch.Tensor, ...]:
    # compile the returns from pylibxc into a tuple of tensors with order given
    # by the keys
    a = lambda v: torch.as_tensor(v.T)
    if family == 1:
        keys = LDA_KEYS
    elif family == 2:
        keys = GGA_KEYS
    elif family == 4:
        keys = MGGA_KEYS
    else:
        raise RuntimeError("Unknown libxc family %d" % family)
    return tuple(a(ret[key]) for key in keys[deriv])

def _get_grad_inps(grad_res: Tuple[torch.Tensor, ...],
                   inps: Tuple[torch.Tensor, ...],
                   derivs: Tuple[torch.Tensor, ...],
                   needs_input_grad: List[bool],
                   deriv_idxs: List[List[int]],
                   spin_idxs: Optional[List[List[Tuple[int, ...]]]] = None) -> Tuple[Optional[torch.Tensor], ...]:
    # calculate the grad_inp from grad_res and given deriv_idxs
    # each row indicates the input, while the column indicates the index in out
    # deriv_idxs[i][j] means that grad_inp[i] += grad_res[j] * derivs[deriv_idxs[i][j]]
    grad_inps: List[Optional[torch.Tensor]] = []
    for i in range(len(deriv_idxs)):
        # if the input does not requires grad, then don't compute
        if not needs_input_grad[i]:
            grad_inps.append(None)
            continue

        grad_inp = torch.zeros_like(inps[i])
        didxs = deriv_idxs[i]
        if spin_idxs is not None:
            sidxs = spin_idxs[i]
        for j in range(len(didxs)):
            if spin_idxs is None:
                grad_inp = grad_inp + grad_res[j] * derivs[didxs[j]]
            else:
                grad_inp = grad_inp + torch.sum(grad_res[j] * derivs[didxs[j]][sidxs[j], :], dim=0)
        grad_inps.append(grad_inp)
    return tuple(grad_inps)

# # keys generator (do not remove!)
# from typing import List, Tuple, Optional
# import collections
# import copy
#
# def __num(n: int) -> str:
#     return "" if n == 1 else str(n)
#
# def __count_name(s: str, name: str) -> int:
#     # returns how many times the name occurs
#     idx = s.find(name)
#     if idx == -1:
#         return 0
#     cidx = idx + len(name)
#     if cidx >= len(s):
#         return 1
#     c = s[cidx]
#     if c.isnumeric():
#         return int(c)
#     else:
#         return 1
#
# def __construct_name(count_name: List[int]) -> str:
#     # construct the name from count_name
#     nderiv = sum(count_name)
#     prefix = f"v{__num(nderiv)}"
#     key = ""
#     for i, name in enumerate(varnames):
#         if count_name[i] == 0:
#             continue
#         key = key + (name + __num(count_name[i]))
#     return prefix + key
#
# def _generate_keys(deriv: int, varnames: List[str]) -> List[str]:
#     # generate keys like:
#     # GGA_KEYS = [["zk"],
#     #             ["vrho", "vsigma"],
#     #             ["v2rho2", "v2rhosigma", "v2sigma2"],
#     #             ["v3rho3", "v3rho2sigma", "v3rhosigma2", "v3sigma3"],
#     #             ["v4rho4", "v4rho3sigma", "v4rho2sigma2", "v4rhosigma3", "v4sigma4"]]
#     if deriv == 0:
#         return ["zk"]
#     prefix = "v%s" % __num(deriv)
#     idxs = [0 for _ in range(deriv)]
#     nvarnames = len(varnames)
#     keys: List[str] = []
#     while True:
#         # construct the key
#         count_idx = collections.Counter(idxs)
#         elmts = sorted(count_idx.keys())
#         key = "".join([(varnames[elmt] + __num(count_idx[elmt])) for elmt in elmts])
#         keys.append(prefix + key)
#         # update the indices
#         update_idx = -1
#         idxs[update_idx] += 1
#         while idxs[update_idx] >= nvarnames:
#             update_idx -= 1
#             if update_idx < -deriv:
#                 break
#             idxs[update_idx] += 1
#         if update_idx < -deriv:
#             break
#         # make sure the idxs not decreasing
#         for ui in range(update_idx + 1, 0):
#             idxs[ui] = idxs[ui - 1]
#     return keys
#
# # deriv_idxs generator code (do not remove!)
# def _generate_pair_deriv_idxs(deriv: int, varnames: List[str]) -> List[List[int]]:
#     # function to generate the derivative index to be paired with grad
#     # not to be executed during the program, only to find the index
#     grad_res_keys = _generate_keys(deriv, varnames)
#     out_deriv_keys = _generate_keys(deriv + 1, varnames)
#     count_names = [[__count_name(grkey, name) for name in varnames] for grkey in grad_res_keys]
#     new_all_names: List[List[str]] = []
#     for i, name in enumerate(varnames):
#         new_names: List[str] = []
#         for crow in count_names:
#             celmt = crow[:]
#             celmt[i] += 1
#             new_names.append(__construct_name(celmt))
#         new_all_names.append(new_names)
#     # find the position of elements in new_all_names in out_deriv_keys
#     res: List[List[int]] = []
#     for new_names in new_all_names:
#         new_row: List[int] = []
#         for new_name in new_names:
#             pos = out_deriv_keys.index(new_name)
#             new_row.append(pos)
#         res.append(new_row)
#     return res
#
# def __generate_vars(varnames: List[str], nspins: List[int]) -> List[Tuple[str, int]]:
#     # generate a tuple of variable name and its spin
#     res: List[Tuple[str, int]] = []
#     for nspin, varname in zip(nspins, varnames):
#         for i in range(nspin):
#             res.append((varname, i))
#     return res
#
# def __generate_idxs(nidxs: int, maxval: int):
#     idxs: List[int] = [0 for _ in range(nidxs)]
#     while True:
#         yield idxs[:]
#         update_idx = -1
#         idxs[update_idx] += 1
#         while idxs[update_idx] >= maxval:
#             update_idx -= 1
#             if update_idx < -nidxs:
#                 break
#             idxs[update_idx] += 1
#         if update_idx < -nidxs:
#             break
#         # make sure the idxs not decreasing
#         for ui in range(update_idx + 1, 0):
#             idxs[ui] = idxs[ui - 1]
#
# import itertools
# def __get_spin_per_var(count: int, nspin: int) -> List[List[int]]:
#     return list(__generate_idxs(count, nspin))
#
# def __generate_spins(cname: List[int], nspins: List[int],
#                      inewvar: Optional[int] = None, newspin: Optional[int] = None) -> List[List[int]]:
#     # cname is name count in a name, nspins is a list of number of spins per varname
#     spins_per_var: List[List[List[int]]] = []
#     for i, (count, nspin) in enumerate(zip(cname, nspins)):
#         spin_per_var: List[List[int]] = __get_spin_per_var(count, nspin) if count > 0 else [[-1]]
#         if inewvar is not None and i == inewvar:
#             spin_per_var = [sp for sp in spin_per_var if newspin in sp]
#         spins_per_var.append(spin_per_var)
#     spins = [sum(p, []) for p in itertools.product(*spins_per_var)]
#     spins = [[s for s in sp if s != -1] for sp in spins]
#     return spins
#
# def _generate_spin_list(deriv: int, varnames: List[str], nspins: List[int]):
#     keys: List[str] = _generate_keys(deriv, varnames)
#     keys_d1: List[str] = _generate_keys(deriv + 1, varnames)
#     cnames: List[List[int]] = [[__count_name(key, name) for name in varnames] for key in keys]
#     cnames_d1: List[List[int]] = [[__count_name(key, name) for name in varnames] for key in keys_d1]
#     varspins: List[Tuple[str, int]] = __generate_vars(varnames, nspins)
#     res: List[List[List[int]]] = []
#     idxs_all = []
#     for var, vspin in varspins:
#         cnames2 = copy.deepcopy(cnames)
#         ivar = varnames.index(var)
#         spins_rows: List[List[int]] = []
#         idxs_row = []
#         for cname in cnames2:  # cname is List[int]
#             cname[ivar] += 1
#             spins = __generate_spins(cname, nspins, ivar, vspin)
#             idx_at_d1 = cnames_d1.index(cname)
#             idxs_row.append(idx_at_d1)
#             spins_d1 = __generate_spins(cnames_d1[idx_at_d1], nspins)
#             # find where spins in spins_d1
#             spins_idxs = tuple(spins_d1.index(spin) for spin in spins)
#             spins_rows.append(spins_idxs)
#         idxs_all.append(idxs_row)
#         res.append(spins_rows)
#     print("deriv_idxs", idxs_all)
#     print("spin_idxs", res)
#     return idxs_all, res
#
# # _generate_pair_deriv_idxs(1, ["rho", "sigma"])
# _generate_spin_list(1, ["rho", "sigma"], [2, 3])
