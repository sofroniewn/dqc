import os
import re
import ctypes
from contextlib import contextmanager
from typing import List, Tuple, Optional, Iterator
import torch
import numpy as np
from dqc.utils.datastruct import AtomCGTOBasis

# Terminology:
# * gauss: one gaussian element (multiple gaussian becomes one shell)
# * shell: one contracted basis
# * ao: shell that has been splitted into its components,
#       e.g. p-shell is splitted into 3 components for cartesian (x, y, z)

NDIM = 3
PTR_RINV_ORIG = 4  # from libcint/src/cint_const.h
BLKSIZE = 128  # same as lib/gto/grid_ao_drv.c

# load the libcint
_curpath = os.path.dirname(os.path.abspath(__file__))
_libcint_path = os.path.join(_curpath, "../../lib/libcint/build/libcint.so")
_libcgto_path = os.path.join(_curpath, "../../lib/libcgto.so")
CINT = ctypes.cdll.LoadLibrary(_libcint_path)
CGTO = ctypes.cdll.LoadLibrary(_libcgto_path)

# Optimizer class
class _cintoptHandler(ctypes.c_void_p):
    def __del__(self):
        try:
            CGTO.CINTdel_optimizer(ctypes.byref(self))
        except AttributeError:
            pass

class LibcintWrapper(object):
    # this class provides the contracted gaussian integrals
    def __init__(self, atombases: List[AtomCGTOBasis], spherical: bool = True) -> None:
        self._atombases = atombases
        self._spherical = spherical
        self._natoms = len(atombases)

        # the libcint lists
        self._ptr_env = 20
        self._atm_list: List[List[int]] = []
        self._env_list: List[float] = [0.0] * self._ptr_env
        self._bas_list: List[List[int]] = []

        # the list of tensors
        self._allpos: List[torch.Tensor] = []
        self._allalphas: List[torch.Tensor] = []
        self._allcoeffs: List[torch.Tensor] = []
        # offset indicating the starting point of i-th shell in r & (alphas & coeffs)
        # in the flatten list above
        self.shell_to_atom: List[int] = []
        self.shell_to_gauss: List[int] = [0]

        # construct the atom, basis, and env lists
        self._nshells = []
        for i, atombasis in enumerate(atombases):
            # modifying most of the parameters above
            ns = self._add_atom_and_basis(i, atombasis)
            self._nshells.append(ns)
        self.nshells_tot = sum(self._nshells)

        # flatten the params list
        self.allpos_params = torch.cat(self._allpos, dim=0)  # (natom, NDIM)
        self.allalphas_params = torch.cat(self._allalphas, dim=0)  # (ntot_gauss)
        self.allcoeffs_params = torch.cat(self._allcoeffs, dim=0)  # (ntot_gauss)

        # convert the lists to numpy (to make it contiguous, Python list is not contiguous)
        self._atm = np.array(self._atm_list, dtype=np.int32, order="C")
        self._bas = np.array(self._bas_list, dtype=np.int32, order="C")
        self._env = np.array(self._env_list, dtype=np.float64, order="C")

        # get the c-pointer of the numpy array
        self.atm_ctypes = self._atm.ctypes.data_as(ctypes.c_void_p)
        self.bas_ctypes = self._bas.ctypes.data_as(ctypes.c_void_p)
        self.env_ctypes = self._env.ctypes.data_as(ctypes.c_void_p)
        self.natm_ctypes = ctypes.c_int(self._atm.shape[0])
        self.nbas_ctypes = ctypes.c_int(self._bas.shape[0])

        # get dtype and device for torch's tensors
        self.dtype = atombases[0].bases[0].alphas.dtype
        self.device = atombases[0].bases[0].alphas.device

        # get the size of the contracted gaussian
        shell_to_aoloc = [0]
        shell_to_nao = []
        for i in range(self.nshells_tot):
            nbasiscontr = self._nbasiscontr(i)
            shell_to_nao.append(nbasiscontr)
            shell_to_aoloc.append(shell_to_aoloc[-1] + nbasiscontr)
        self.shell_to_aoloc = np.array(shell_to_aoloc, dtype=np.int32)
        self.nao_tot = self.shell_to_aoloc[-1]

        # get the mapping from ao to atom index
        self.ao_to_atom = torch.zeros((self.nao_tot,), dtype=torch.long)
        for i in range(self.nshells_tot):
            idx = self.shell_to_aoloc[i]
            self.ao_to_atom[idx: idx + shell_to_nao[i]] = self.shell_to_atom[i]

    def _add_atom_and_basis(self, iatom: int, atombasis: AtomCGTOBasis) -> int:
        # construct the atom first
        assert atombasis.pos.numel() == NDIM, "Please report this bug in Github"
        #                      charge           ptr_coord       nucl model (unused for standard nucl model)
        self._atm_list.append([atombasis.atomz, self._ptr_env, 1, self._ptr_env + NDIM, 0, 0])
        self._env_list.extend(atombasis.pos)
        self._ptr_env += NDIM
        self._env_list.extend([0.0])
        self._ptr_env += 1

        # then construct the basis
        for basis in atombasis.bases:
            assert basis.alphas.shape == basis.coeffs.shape and basis.alphas.ndim == 1,\
                "Please report this bug in Github"

            normcoeff = self._normalize_basis(basis.alphas, basis.coeffs, basis.angmom)

            ngauss = len(basis.alphas)
            #                      iatom, angmom,       ngauss, ncontr, kappa, ptr_exp
            self._bas_list.append([iatom, basis.angmom, ngauss, 1, 0, self._ptr_env,
                                   # ptr_coeffs,           unused
                                   self._ptr_env + ngauss, 0])
            self._env_list.extend(basis.alphas)
            self._env_list.extend(normcoeff)
            self._ptr_env += 2 * ngauss

            # add the basis coeffs and alphas to the flat list and update the offset
            self._allalphas.append(basis.alphas)
            self._allcoeffs.append(normcoeff)
            self.shell_to_gauss.append(self.shell_to_gauss[-1] + ngauss)
            self.shell_to_atom.append(iatom)

        # add the atom position
        self._allpos.append(atombasis.pos.unsqueeze(0))
        return len(atombasis.bases)

    def _normalize_basis(self, alphas: torch.Tensor, coeffs: torch.Tensor,
                         angmom: int) -> torch.Tensor:
        # the normalization is obtained from CINTgto_norm from
        # libcint/src/misc.c, or
        # https://github.com/sunqm/libcint/blob/b8594f1d27c3dad9034984a2a5befb9d607d4932/src/misc.c#L80

        # precomputed factor:
        # 2 ** (2 * angmom + 3) * factorial(angmom + 1) * / \
        # (factorial(angmom * 2 + 2) * np.sqrt(np.pi)))
        factor = [
            2.256758334191025,  # 0
            1.5045055561273502,  # 1
            0.6018022224509401,  # 2
            0.17194349212884005,  # 3
            0.03820966491752001,  # 4
            0.006947211803185456,  # 5
            0.0010688018158746854,  # 6
        ]
        return coeffs * torch.sqrt(factor[angmom] * (2 * alphas) ** (angmom + 1.5))

    def overlap(self) -> torch.Tensor:
        return self._int1e("ovlp")

    def kinetic(self) -> torch.Tensor:
        return self._int1e("kin")

    def nuclattr(self) -> torch.Tensor:
        return self._int1e("nuc", True)

    def elrep(self) -> torch.Tensor:
        return self._int2e("ar12b")

    def eval_gto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (nao, ngrid)
        return self._evalgto("", rgrid)

    def eval_gradgto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (ndim, nao, ngrid)
        return self._evalgto("ip", rgrid)

    def eval_laplgto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (nao, ngrid)
        return self._evalgto("lapl", rgrid)

    ################ integrals to construct the operator ################
    def _int1e(self, shortname: str, nuc: bool = False) -> torch.Tensor:
        # one electron integral (overlap, kinetic, and nuclear attraction)
        return _Int1eFunction.apply(
            self.allcoeffs_params, self.allalphas_params, self.allpos_params,
            self.allpos_params,
            self, shortname)

    def _int2e(self, shortname: str) -> torch.Tensor:
        # two electron integral (coulomb)
        return _Int2eFunction.apply(
            self.allcoeffs_params, self.allalphas_params, self.allpos_params,
            self, shortname)

    ################ one electrons integral for all basis pairs ################
    def calc_all_int1e_internal(self, shortname: str) -> torch.Tensor:
        # calculation of all basis pairs of 1-electron integrals
        # no gradient is propagated
        # return (ndim^n, nao, nao)

        opname = self._get_all_intxe_name(1, shortname)
        operator = getattr(CINT, opname)
        optimizer = self._get_intxe_optimizer(opname)

        # prepare the output
        outshape, ncomp = self._get_all_intxe_outshape(1, shortname)
        out = np.empty(outshape, dtype=np.float64)

        shls_slice = (0, self.nshells_tot) * 2
        drv = CGTO.GTOint2c
        drv(operator,
            out.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(ncomp),
            ctypes.c_int(0),  # do not assume hermitian
            (ctypes.c_int * len(shls_slice))(*shls_slice),
            self.shell_to_aoloc.ctypes.data_as(ctypes.c_void_p),
            optimizer,
            self.atm_ctypes, self.natm_ctypes,
            self.bas_ctypes, self.nbas_ctypes,
            self.env_ctypes)

        out = np.swapaxes(out, -2, -1)
        # TODO: check if we need to do the lines below for 3rd order grad and higher
        # if out.ndim > 2:
        #     out = np.moveaxis(out, -3, 0)
        out_tensor = torch.as_tensor(out, dtype=self.dtype, device=self.device)
        return out_tensor

    ################ two electrons integral for all basis pairs ################
    def calc_all_int2e_internal(self, shortname: str) -> torch.Tensor:
        # calculation of all 2-electron integrals
        # no gradient is propagated
        # return (ndim^n, nao, nao, nao, nao)

        opname = self._get_all_intxe_name(2, shortname)
        operator = getattr(CINT, opname)
        optimizer = self._get_intxe_optimizer(opname)

        # prepare the output
        outshape, ncomp = self._get_all_intxe_outshape(2, shortname)
        out = np.empty(outshape, dtype=np.float64)

        drv = CGTO.GTOnr2e_fill_drv
        fill = CGTO.GTOnr2e_fill_s1
        prescreen = ctypes.POINTER(ctypes.c_void_p)()
        shls_slice = (0, self.nshells_tot) * 4
        drv(operator, fill, prescreen,
            out.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(ncomp),
            (ctypes.c_int * 8)(*shls_slice),
            self.shell_to_aoloc.ctypes.data_as(ctypes.c_void_p),
            optimizer,
            self.atm_ctypes, self.natm_ctypes,
            self.bas_ctypes, self.nbas_ctypes,
            self.env_ctypes)

        # # reverse the last 4 axes
        # out = np.swapaxes(out, -4, -2)
        # out = np.swapaxes(out, -3, -1)
        out_tensor = torch.as_tensor(out, dtype=self.dtype, device=self.device)
        return out_tensor

    ################ helpers for all pairs integrals ################
    def _get_all_intxe_name(self, x: int, shortname: str) -> str:
        suffix = ("_" + shortname) if shortname != "" else shortname
        cartsph = "sph" if self._spherical else "cart"
        return "int%de%s_%s" % (x, suffix, cartsph)

    def _get_intxe_optimizer(self, opname: str) -> ctypes.c_void_p:
        # setup the optimizer
        cintopt = ctypes.POINTER(ctypes.c_void_p)()
        optname = opname.replace("_cart", "").replace("_sph", "") + "_optimizer"
        copt = getattr(CINT, optname)
        copt(ctypes.byref(cintopt),
             self.atm_ctypes, self.natm_ctypes,
             self.bas_ctypes, self.nbas_ctypes, self.env_ctypes)
        opt = ctypes.cast(cintopt, _cintoptHandler)
        return opt

    def _get_all_intxe_outshape(self, x: int, shortname: str) -> Tuple[Tuple[int, ...], int]:
        n_ip = _calc_pattern_occurence(shortname, "ip")
        outshape = (NDIM, ) * n_ip + (self.nao_tot, ) * (2 * x)
        ncomp = NDIM ** n_ip
        return outshape, ncomp

    @contextmanager
    def _centre_on_r(self, ratom: torch.Tensor) -> Iterator:
        # ratom: (ndim,)
        try:
            prev_centre = self._env[PTR_RINV_ORIG : PTR_RINV_ORIG + NDIM]
            self._env[PTR_RINV_ORIG : PTR_RINV_ORIG + NDIM] = ratom.detach().numpy()
            yield
        finally:
            self._env[PTR_RINV_ORIG : PTR_RINV_ORIG + NDIM] = prev_centre

    ################ evaluation of gto orbitals ################
    def _evalgto(self, shortname: str, rgrid: torch.Tensor) -> torch.Tensor:
        # expand ao_to_atom to have shape of (nao, ndim)
        ao_to_atom = self.ao_to_atom.unsqueeze(-1).expand(-1, NDIM)

        # rgrid: (ngrid, ndim)
        return _EvalGTO.apply(
            # tensors
            self.allalphas_params,
            self.allcoeffs_params,
            self.allpos_params,
            rgrid,

            # nontensors or int tensors
            ao_to_atom,
            self,
            shortname)

    def _get_evalgto_opname(self, shortname: str) -> str:
        sname = ("_" + shortname) if (shortname != "") else ""
        suffix = "_sph" if self._spherical else "_cart"
        return "GTOval%s%s" % (sname, suffix)

    def _get_evalgto_outshape(self, shortname: str, nao: int, ngrid: int) -> List[int]:
        # count "ip" only at the beginning
        n_ip = len(re.findall(r"^(?:ip)*(?:ip)?", shortname)[0]) // 2
        return ([NDIM] * n_ip) + [nao, ngrid]

    def _get_evalgto_derivname(self, shortname: str, derivmode: str):
        if derivmode == "r":
            return "ip%s" % shortname
        else:
            raise RuntimeError("Unknown derivmode: %s" % derivmode)

    def eval_gto_internal(self, shortname: str, rgrid: torch.Tensor) -> torch.Tensor:
        # NOTE: this method do not propagate gradient and should only be used
        # in this file only

        # rgrid: (ngrid, ndim)
        # returns: (*, nao, ngrid)

        ngrid = rgrid.shape[0]
        nshells = self.nshells_tot
        nao = self.nao_tot
        opname = self._get_evalgto_opname(shortname)
        outshape = self._get_evalgto_outshape(shortname, nao, ngrid)

        out = np.empty(outshape, dtype=np.float64)
        non0tab = np.ones(((ngrid + BLKSIZE - 1) // BLKSIZE, nshells),
                          dtype=np.int8)

        # TODO: check if we need to transpose it first?
        rgrid = rgrid.contiguous()
        coords = np.asarray(rgrid, dtype=np.float64, order='F')
        ao_loc = np.asarray(self.shell_to_aoloc, dtype=np.int32)

        c_shls = (ctypes.c_int * 2)(0, nshells)
        c_ngrid = ctypes.c_int(ngrid)

        # evaluate the orbital
        operator = getattr(CGTO, opname)
        operator.restype = ctypes.c_double
        operator(c_ngrid, c_shls,
                 ao_loc.ctypes.data_as(ctypes.c_void_p),
                 out.ctypes.data_as(ctypes.c_void_p),
                 coords.ctypes.data_as(ctypes.c_void_p),
                 non0tab.ctypes.data_as(ctypes.c_void_p),
                 self.atm_ctypes, self.natm_ctypes,
                 self.bas_ctypes, self.nbas_ctypes,
                 self.env_ctypes)

        out = torch.tensor(out, dtype=self.dtype, device=self.device)
        return out

    ################ misc functions ################
    def _nbasiscontr(self, sh: int) -> int:
        if self._spherical:
            op = CINT.CINTcgto_spheric
        else:
            op = CINT.CINTcgto_cart
        return op(ctypes.c_int(sh), self.bas_ctypes)

############### autograd function ###############
class _Int1eFunction(torch.autograd.Function):
    # wrapper class to provide the gradient of the one-e integrals
    @staticmethod
    def forward(ctx,  # type: ignore
                allcoeffs: torch.Tensor, allalphas: torch.Tensor, allposs: torch.Tensor,
                ratoms: torch.Tensor,
                wrapper: LibcintWrapper, shortname: str) -> torch.Tensor:
        # allcoeffs: (ngauss_tot,)
        # allalphas: (ngauss_tot,)
        # allposs: (natom, ndim)
        # ratoms: (natom, ndim) if contains "nuc" or (ndim,) if contains "rinv"
        #         ratoms is only meaningful if shortname contains "rinv" or "nuc"
        # in "rinv", ratoms becomes the centre

        # those tensors are not used in the forward calculation, but required
        # for backward propagation
        if "rinv" in shortname:
            assert ratoms.ndim == 1 and ratoms.shape[0] == NDIM
            with wrapper._centre_on_r(ratoms):
                out_tensor = wrapper.calc_all_int1e_internal(shortname)
        else:
            out_tensor = wrapper.calc_all_int1e_internal(shortname)
        ctx.save_for_backward(allcoeffs, allalphas, allposs, ratoms)
        ctx.other_info = (wrapper, shortname)
        return out_tensor  # (..., nao, nao)

    @staticmethod
    def backward(ctx, grad_out: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        # grad_out: (..., nao, nao)
        allcoeffs, allalphas, allposs, ratoms = ctx.saved_tensors
        wrapper, shortname = ctx.other_info
        nao = grad_out.shape[-1]

        # TODO: to be completed (???)
        grad_allcoeffs: Optional[torch.Tensor] = None
        grad_allalphas: Optional[torch.Tensor] = None

        grad_allposs: Optional[torch.Tensor] = None
        if allposs.requires_grad:
            grad_allposs = torch.zeros_like(allposs)  # (natom, ndim)
            grad_allpossT = grad_allposs.transpose(-2, -1)  # (ndim, natom)

            deriv_shortname  = _get_int1e_deriv_shortname(shortname, "r1")
            deriv_shortnameT = _get_int1e_deriv_shortname(shortname, "r2")
            dout_dpos = _Int1eFunction.apply(
                *ctx.saved_tensors, wrapper, deriv_shortname)  # (ndim, ..., nao, nao)

            # if deriv_shortname and deriv_shortnameT can be just flipped, then
            # no need to calculate the other one
            if _int1e_shortname_equiv(deriv_shortname, deriv_shortnameT):
                dout_dposT = dout_dpos.transpose(-2, -1)
            else:
                dout_dposT = _Int1eFunction.apply(
                    *ctx.saved_tensors, wrapper, deriv_shortnameT)

            # negative because the integral calculates the nabla w.r.t. the
            # spatial coordinate, not the basis central position
            neg_grad_out = -grad_out
            grad_dpos  = neg_grad_out * dout_dpos
            grad_dposT = neg_grad_out * dout_dposT
            ndim = dout_dpos.shape[0]
            grad_dpos_j = grad_dpos .reshape(ndim, -1, nao, nao).sum(dim=-3).sum(dim=-1)  # (ndim, nao)
            grad_dpos_i = grad_dposT.reshape(ndim, -1, nao).sum(dim=-2)
            grad_dpos_ij = grad_dpos_i + grad_dpos_j

            # grad_allpossT is only a view of grad_allposs, so the operation below
            # also changes grad_allposs
            ao_to_atom = wrapper.ao_to_atom.expand(ndim, -1)
            grad_allpossT.scatter_add_(dim=-1, index=ao_to_atom, src=grad_dpos_ij)

        # calculate the gradient of ratoms for "nuc" or "rinv" integration
        grad_ratoms: Optional[torch.Tensor] = None
        if ratoms.requires_grad and "nuc" in shortname:
            # ratoms: (natoms, ndim)
            natoms = ratoms.shape[0]
            sname_rinv = shortname.replace("nuc", "rinv")
            sname_deriv1 = _get_int1e_deriv_shortname(sname_rinv, "r1")
            sname_deriv2 = _get_int1e_deriv_shortname(sname_rinv, "r2")
            sname12_equiv = _int1e_shortname_equiv(sname_deriv1, sname_deriv2)

            grad_ratoms = torch.empty_like(ratoms)
            for i in range(natoms):
                atomz = wrapper._atm[i, 0]  # 0 is the charge position in _atm

                # calculate where the derivative is on the left basis
                dout_datpos1 = _Int1eFunction.apply(
                    allcoeffs, allalphas, allposs, ratoms[i],
                    wrapper, sname_deriv1)  # (ndim, ..., nao, nao)

                # calculate the factor where the derivative is on the right
                if sname12_equiv:
                    dout_datpos2 = dout_datpos1.transpose(-2, -1)
                else:
                    dout_datpos2 = _Int1eFunction.apply(
                        allcoeffs, allalphas, allposs, ratoms[i],
                        wrapper, sname_deriv2)

                grad_datpos = grad_out * (dout_datpos1 + dout_datpos2)
                grad_datpos = grad_datpos.reshape(grad_datpos.shape[0], -1).sum(dim=-1)
                grad_ratoms[i] = -atomz * grad_datpos

        elif ratoms.requires_grad and "rinv" in shortname:
            # ratoms: (ndim)
            sname_deriv1 = _get_int1e_deriv_shortname(shortname, "r1")
            sname_deriv2 = _get_int1e_deriv_shortname(shortname, "r2")

            dout_datpos1 = _Int1eFunction.apply(
                *ctx.saved_tensors, wrapper, sname_deriv1)

            if _int1e_shortname_equiv(sname_deriv1, sname_deriv2):
                dout_datpos2 = dout_datpos1.transpose(-2, -1)
            else:
                dout_datpos2 = _Int1eFunction.apply(
                    *ctx.saved_tensors, wrapper, sname_deriv2)

            grad_datpos = grad_out * (dout_datpos1 + dout_datpos2)
            grad_ratoms = grad_datpos.reshape(grad_datpos.shape[0], -1).sum(dim=-1)

        return grad_allcoeffs, grad_allalphas, grad_allposs, grad_ratoms, \
            None, None

class _Int2eFunction(torch.autograd.Function):
    # wrapper class to provide the gradient of the one-e integrals
    @staticmethod
    def forward(ctx,  # type: ignore
                allcoeffs: torch.Tensor, allalphas: torch.Tensor, allposs: torch.Tensor,
                wrapper: LibcintWrapper, shortname: str) -> torch.Tensor:
        # allcoeffs: (ngauss_tot,)
        # allalphas: (ngauss_tot,)
        # allposs: (natom, ndim)

        out_tensor = wrapper.calc_all_int2e_internal(shortname)  # (..., nao^4)
        ctx.save_for_backward(allcoeffs, allalphas, allposs)
        ctx.other_info = (wrapper, shortname)
        return out_tensor  # (..., nao^4)

    @staticmethod
    def backward(ctx, grad_out: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        allcoeffs, allalphas, allposs = ctx.saved_tensors
        wrapper, shortname = ctx.other_info
        nao = grad_out.shape[-1]

        # TODO: to be completed (???)
        grad_allcoeffs: Optional[torch.Tensor] = None
        grad_allalphas: Optional[torch.Tensor] = None

        # calculate the gradient w.r.t. positions
        grad_allposs: Optional[torch.Tensor] = None
        if allposs.requires_grad:
            grad_allposs = torch.zeros_like(allposs)  # (natom, ndim)
            grad_allpossT = grad_allposs.transpose(-2, -1)  # (ndim, natom)

            sname_deriv_a1 = _get_int2e_deriv_shortname(shortname, "ra1")
            sname_deriv_a2 = _get_int2e_deriv_shortname(shortname, "ra2")
            sname_deriv_b1 = _get_int2e_deriv_shortname(shortname, "rb1")
            sname_deriv_b2 = _get_int2e_deriv_shortname(shortname, "rb2")

            dout_dpos_a1 = _Int2eFunction.apply(
                *ctx.saved_tensors, wrapper, sname_deriv_a1)  # (ndim, ..., nao^4)

            # calculate the derivative at the left's 2nd position
            axes_relation_a12 = _int2e_shortname_equiv(sname_deriv_a1, sname_deriv_a2)
            if axes_relation_a12 is not None:
                dout_dpos_a2 = _transpose(dout_dpos_a1, axes_relation_a12)
            else:
                dout_dpos_a2 = _Int2eFunction.apply(
                    *ctx.saved_tensors, wrapper, sname_deriv_a2)  # (ndim, ..., nao^4)

            # calculate the derivative at the right's 1st position
            axes_relation_a1b1 = _int2e_shortname_equiv(sname_deriv_a1, sname_deriv_b1)
            axes_relation_a2b1 = _int2e_shortname_equiv(sname_deriv_a2, sname_deriv_b1)
            if axes_relation_a1b1 is not None:
                dout_dpos_b1 = _transpose(dout_dpos_a1, axes_relation_a1b1)
            elif axes_relation_a2b1 is not None:
                dout_dpos_b1 = _transpose(dout_dpos_a2, axes_relation_a2b1)
            else:
                dout_dpos_b1 = _Int2eFunction.apply(
                    *ctx.saved_tensors, wrapper, sname_deriv_b1)  # (ndim, ..., nao^4)

            # calculate the derivative at the right's 2nd
            axes_relation_b12  = _int2e_shortname_equiv(sname_deriv_b1, sname_deriv_b2)
            axes_relation_a1b2 = _int2e_shortname_equiv(sname_deriv_a1, sname_deriv_b2)
            axes_relation_a2b2 = _int2e_shortname_equiv(sname_deriv_a2, sname_deriv_b2)
            if axes_relation_b12 is not None:
                dout_dpos_b2 = _transpose(dout_dpos_b1, axes_relation_b12)
            elif axes_relation_a1b2 is not None:
                dout_dpos_b2_2 = _transpose(dout_dpos_a1, axes_relation_a1b2)
            elif axes_relation_a2b2 is not None:
                dout_dpos_b2 = _transpose(dout_dpos_a2, axes_relation_a2b2)
            else:
                dout_dpos_b2 = _Int2eFunction.apply(
                    *ctx.saved_tensors, wrapper, sname_deriv_b2)  # (ndim, ..., nao^4)

            # negative because the integral calculates the nabla w.r.t. the
            # spatial coordinate, not the basis central position
            ndim = dout_dpos_a1.shape[0]
            shape = (ndim, -1, nao, nao, nao, nao)
            grad_out2 = grad_out.reshape(*shape[1:])
            grad_pos_a1 = -torch.einsum("dzijkl,zijkl->di", dout_dpos_a1.reshape(*shape), grad_out2)
            grad_pos_a2 = -torch.einsum("dzijkl,zijkl->dj", dout_dpos_a2.reshape(*shape), grad_out2)
            grad_pos_b1 = -torch.einsum("dzijkl,zijkl->dk", dout_dpos_b1.reshape(*shape), grad_out2)
            grad_pos_b2 = -torch.einsum("dzijkl,zijkl->dl", dout_dpos_b2.reshape(*shape), grad_out2)
            grad_pos_all = grad_pos_a1 + grad_pos_a2 + grad_pos_b1 + grad_pos_b2

            ao_to_atom = wrapper.ao_to_atom.expand(ndim, -1)
            grad_allpossT.scatter_add_(dim=-1, index=ao_to_atom, src=grad_pos_all)

        return grad_allcoeffs, grad_allalphas, grad_allposs, \
            None, None

class _EvalGTO(torch.autograd.Function):
    @staticmethod
    def forward(ctx,  # type: ignore
                # tensors not used in calculating the forward, but required
                # for the backward propagation
                alphas: torch.Tensor,  # (ngauss_tot)
                coeffs: torch.Tensor,  # (ngauss_tot)
                pos: torch.Tensor,  # (natom, ndim)

                # tensors used in forward
                rgrid: torch.Tensor,  # (ngrid, ndim)

                # other non-tensor info
                ao_to_atom: torch.Tensor,  # int tensor (nao, ndim)
                wrapper: LibcintWrapper,
                shortname: str) -> torch.Tensor:

        res = wrapper.eval_gto_internal(shortname, rgrid)  # (*, nao, ngrid)
        ctx.save_for_backward(alphas, coeffs, pos, rgrid)
        ctx.other_info = (ao_to_atom, wrapper, shortname)
        return res

    @staticmethod
    def backward(ctx, grad_res: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        # grad_res: (*, nao, ngrid)
        ao_to_atom, wrapper, shortname = ctx.other_info
        alphas, coeffs, pos, rgrid = ctx.saved_tensors

        # TODO: implement the gradient w.r.t. alphas and coeffs
        grad_alphas = None
        grad_coeffs = None

        # calculate the gradient w.r.t. basis' pos and rgrid
        grad_pos = None
        grad_rgrid = None
        if rgrid.requires_grad or pos.requires_grad:
            opsname = wrapper._get_evalgto_derivname(shortname, "r")
            dresdr = _EvalGTO.apply(*ctx.saved_tensors,
                                    ao_to_atom, wrapper, opsname)  # (ndim, *, nao, ngrid)
            grad_r = dresdr * grad_res  # (ndim, *, nao, ngrid)

            if rgrid.requires_grad:
                grad_rgrid = grad_r.reshape(dresdr.shape[0], -1, dresdr.shape[-1])
                grad_rgrid = grad_rgrid.sum(dim=1).transpose(-2, -1)  # (ngrid, ndim)

            if pos.requires_grad:
                grad_rao = torch.movedim(grad_r, -2, 0)  # (nao, ndim, *, ngrid)
                grad_rao = -grad_rao.reshape(*grad_rao.shape[:2], -1).sum(dim=-1)  # (nao, ndim)
                grad_pos = torch.zeros_like(pos)  # (natom, ndim)
                grad_pos.scatter_add_(dim=0, index=ao_to_atom, src=grad_rao)

        return grad_alphas, grad_coeffs, grad_pos, grad_rgrid, \
            None, None, None, None, None

############### name derivation manager functions ###############
def _get_int1e_deriv_shortname(shortname: str, derivmode: str) -> str:
    # get the operation required for the derivation of the integration operator
    if derivmode == "r1":
        return "ip%s" % shortname
    elif derivmode == "r2":
        return "%sip" % shortname
    else:
        raise RuntimeError("Unknown derivmode: %s" % derivmode)

def _int1e_shortname_equiv(s1: str, s2: str) -> bool:
    # check if the shortname 1 and 2 is actually a transpose of each other
    n_ip_start1 = _calc_pattern_occurence(s1, "ip", at_start=True)
    n_ip_end1 = _calc_pattern_occurence(s1, "ip", at_end=True)
    n_ip_start2 = _calc_pattern_occurence(s2, "ip", at_start=True)
    n_ip_end2 = _calc_pattern_occurence(s2, "ip", at_end=True)
    return min(n_ip_start1, n_ip_end1) == min(n_ip_start2, n_ip_end2) and \
        max(n_ip_start1, n_ip_end1) == max(n_ip_start2, n_ip_end2)

def _get_int2e_deriv_shortname(shortname: str, derivmode: str) -> str:
    if derivmode == "ra1":
        return "ip%s" % shortname
    elif derivmode == "ra2":
        # insert after the first "a"
        idx_a = shortname.find("a")
        return shortname[:idx_a + 1] + "ip" + shortname[idx_a + 1:]
    elif derivmode == "rb1":
        # insert before the last "b"
        idx_b = shortname.rfind("b")
        return shortname[:idx_b] + "ip" + shortname[idx_b:]
    elif derivmode == "rb2":
        return "%sip" % shortname
    else:
        raise RuntimeError("Unknown derivmode: %s" % derivmode)

def _int2e_shortname_equiv(s1: str, s2: str) -> Optional[List[Tuple[int, int]]]:
    # check if the integration s2 can be achieved by transposing s1
    # returns None if it cannot.
    # returns the list of two dims if it can for the transpose-path of s1
    # to get the same result as s2
    p1 = _int2e_parse_pattern(s1, "ip")
    p2 = _int2e_parse_pattern(s2, "ip")
    res: List[Tuple[int, int]] = []
    offset = 100
    l1 = max(p1[0], p1[1]) * offset + min(p1[0], p1[1])
    r1 = max(p1[2], p1[3]) * offset + min(p1[2], p1[3])
    l2 = max(p2[0], p2[1]) * offset + min(p2[0], p2[1])
    r2 = max(p2[2], p2[3]) * offset + min(p2[2], p2[3])
    ll_equal = l1 == l2
    rr_equal = r1 == r2
    lr_equal = l1 == r2
    rl_equal = r1 == l2

    # check if it cannot be matched
    if not ((ll_equal and rr_equal) or (lr_equal and rl_equal)):
        return None

    if not (ll_equal and rr_equal):  # (lr_equal and rl_equal)
        # swap the right and half of pattern 2
        res.extend([(-4, -2), (-3, -1)])
        if p1[0] != p2[2]:
            res.append((-1, -2))  # swap the right part
        if p1[2] != p2[0]:
            res.append((-3, -4))  # swap the left part
    else:  # (ll_equal and rr_equal)
        if p1[0] != p2[0]:
            res.append((-3, -4))  # swap the left part
        if p1[2] != p2[2]:
            res.append((-1, -2))  # swap the right part
    return res

def _int2e_parse_pattern(s: str, pattern: str) -> List[int]:
    s = s.replace("r12", "|").replace("a", "|").replace("b", "|")
    return [_calc_pattern_occurence(c, pattern) for c in s.split("|")]

def _calc_pattern_occurence(s: str, pattern: str,
                            at_start: bool = False, at_end: bool = False) -> int:
    # calculate the occurence of a pattern in string s at the start or at the end
    # of the string
    if at_start:
        re_pattern = r"^(?:{pattern})*(?:{pattern})?".format(pattern=pattern)
        return len(re.findall(re_pattern, s)[0]) // len(pattern)
    elif at_end:
        re_pattern = r"(?:{pattern})?(?:{pattern})*$".format(pattern=pattern)
        return len(re.findall(re_pattern, s)[0]) // len(pattern)
    else:
        re_pattern = r"({pattern})".format(pattern=pattern)
        return len(re.findall(re_pattern, s))

def _transpose(a: torch.Tensor, axes: List[Tuple[int, int]]) -> torch.Tensor:
    # perform the transpose of two axes for tensor a
    for axis2 in axes:
        a = a.transpose(*axis2)
    return a

if __name__ == "__main__":
    print(_int2e_shortname_equiv("ipar12b", "ipar12b"))
    # from dqc.api.loadbasis import loadbasis
    # dtype = torch.float64
    # bases = loadbasis("3:6-311++G**", dtype=dtype)
    # pos1 = torch.tensor([0.0, 0.0, 0.0], dtype=dtype)
    # pos2 = torch.tensor([0.0, 0.0, 1.0], dtype=dtype)
    # atom1 = AtomCGTOBasis(atomz=3, bases=bases, pos=pos1)
    # atom2 = AtomCGTOBasis(atomz=3, bases=bases, pos=pos2)
    # wrapper = LibcintWrapper([atom1, atom2], spherical=True)
    #
    # out = wrapper.calc_all_int1e_internal("ipovlp")
    # print(out)
    # print(out.shape)