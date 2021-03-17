from __future__ import annotations
from functools import lru_cache
from contextlib import contextmanager
from typing import List, Tuple, Iterator
import torch
import numpy as np
from dqc.utils.datastruct import AtomCGTOBasis, CGTOBasis
from dqc.hamilton.intor.utils import np2ctypes, int2ctypes, NDIM, CINT
from dqc.system.tools import Lattice

__all__ = ["LibcintWrapper", "SubsetLibcintWrapper"]

# Terminology:
# * gauss: one gaussian element (multiple gaussian becomes one shell)
# * shell: one contracted basis (the same contracted gaussian for different atoms
#          counted as different shells)
# * ao: shell that has been splitted into its components,
#       e.g. p-shell is splitted into 3 components for cartesian (x, y, z)

PTR_RINV_ORIG = 4  # from libcint/src/cint_const.h

class LibcintWrapper(object):
    def __init__(self, atombases: List[AtomCGTOBasis], spherical: bool = True,
                 basis_normalized: bool = False, lattice: Optional[Lattice] = None) -> None:
        self._atombases = atombases
        self._spherical = spherical
        self._basis_normalized = basis_normalized
        self._fracz = False
        self._natoms = len(atombases)
        self._lattice = lattice

        # get dtype and device for torch's tensors
        self.dtype = atombases[0].bases[0].alphas.dtype
        self.device = atombases[0].bases[0].alphas.device

        # construct _atm, _bas, and _env as well as the parameters
        ptr_env = 20  # initial padding from libcint
        atm_list: List[List[int]] = []
        env_list: List[float] = [0.0] * ptr_env
        bas_list: List[List[int]] = []
        allpos: List[torch.Tensor] = []
        allalphas: List[torch.Tensor] = []
        allcoeffs: List[torch.Tensor] = []
        shell_to_atom: List[int] = []
        ngauss_at_shell: List[int] = []

        # constructing the triplet lists and also collecting the parameters
        nshells = 0
        for iatom, atombasis in enumerate(atombases):
            # construct the atom environment
            assert atombasis.pos.numel() == NDIM, "Please report this bug in Github"
            atomz = atombasis.atomz
            #                charge    ptr_coord, nucl model (unused for standard nucl model)
            atm_list.append([int(atomz), ptr_env, 1, ptr_env + NDIM, 0, 0])
            env_list.extend(atombasis.pos.detach())
            env_list.append(0.0)
            ptr_env += NDIM + 1

            # check if the atomz is fractional
            if isinstance(atomz, float) or \
                    (isinstance(atomz, torch.Tensor) and atomz.is_floating_point()):
                self._fracz = True

            # add the atom position into the parameter list
            # TODO: consider moving allpos into shell
            allpos.append(atombasis.pos.unsqueeze(0))

            nshells += len(atombasis.bases)
            shell_to_atom.extend([iatom] * len(atombasis.bases))

            # then construct the basis
            for shell in atombasis.bases:
                assert shell.alphas.shape == shell.coeffs.shape and shell.alphas.ndim == 1,\
                    "Please report this bug in Github"
                normcoeff = self._normalize_basis(basis_normalized, shell.alphas, shell.coeffs, shell.angmom)
                ngauss = len(shell.alphas)
                #                iatom, angmom,       ngauss, ncontr, kappa, ptr_exp
                bas_list.append([iatom, shell.angmom, ngauss, 1, 0, ptr_env,
                                 # ptr_coeffs,           unused
                                 ptr_env + ngauss, 0])
                env_list.extend(shell.alphas.detach())
                env_list.extend(normcoeff.detach())
                ptr_env += 2 * ngauss

                # add the alphas and coeffs to the parameters list
                allalphas.append(shell.alphas)
                allcoeffs.append(normcoeff)
                ngauss_at_shell.append(ngauss)

        # compile the parameters of this object
        self._allpos_params = torch.cat(allpos, dim=0)  # (natom, NDIM)
        self._allalphas_params = torch.cat(allalphas, dim=0)  # (ntot_gauss)
        self._allcoeffs_params = torch.cat(allcoeffs, dim=0)  # (ntot_gauss)

        # convert the lists to numpy to make it contiguous (Python lists are not contiguous)
        self._atm = np.array(atm_list, dtype=np.int32, order="C")
        self._bas = np.array(bas_list, dtype=np.int32, order="C")
        self._env = np.array(env_list, dtype=np.float64, order="C")

        # construct the full shell mapping
        shell_to_aoloc = [0]
        ao_to_shell: List[int] = []
        ao_to_atom: List[int] = []
        for i in range(nshells):
            nao_at_shell_i = self._nao_at_shell(i)
            shell_to_aoloc_i = shell_to_aoloc[-1] + nao_at_shell_i
            shell_to_aoloc.append(shell_to_aoloc_i)
            ao_to_shell.extend([i] * nao_at_shell_i)
            ao_to_atom.extend([shell_to_atom[i]] * nao_at_shell_i)

        self._ngauss_at_shell_list = ngauss_at_shell
        self._shell_to_aoloc = np.array(shell_to_aoloc, dtype=np.int32)
        self._shell_idxs = (0, nshells)
        self._ao_to_shell = torch.tensor(ao_to_shell, dtype=torch.long, device=self.device)
        self._ao_to_atom = torch.tensor(ao_to_atom, dtype=torch.long, device=self.device)

    @property
    def natoms(self) -> int:
        # return the number of atoms in the environment
        return self._natoms

    @property
    def fracz(self) -> bool:
        # indicating whether we are working with fractional z
        return self._fracz

    @property
    def basis_normalized(self) -> bool:
        return self._basis_normalized

    @property
    def lattice(self) -> Lattice:
        assert self._lattice is not None
        return self._lattice

    @property
    def spherical(self) -> bool:
        # returns whether the basis is in spherical coordinate (otherwise, it
        # is in cartesian coordinate)
        return self._spherical

    @property
    def atombases(self) -> List[AtomCGTOBasis]:
        return self._atombases

    @property
    def atm_bas_env(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        # returns the triplet lists, i.e. atm, bas, env
        # this shouldn't change in the sliced wrapper
        return self._atm, self._bas, self._env

    @property
    def params(self) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # returns all the parameters of this object
        # this shouldn't change in the sliced wrapper
        return self._allcoeffs_params, self._allalphas_params, self._allpos_params

    @property
    def shell_idxs(self) -> Tuple[int, int]:
        # returns the lower and upper indices of the shells of this object
        # in the absolute index (upper is exclusive)
        return self._shell_idxs

    @property
    def full_shell_to_aoloc(self) -> np.ndarray:
        # returns the full array mapping from shell index to absolute ao location
        # the atomic orbital absolute index of i-th shell is given by
        # (self.full_shell_to_aoloc[i], self.full_shell_to_aoloc[i + 1])
        # if this object is a subset, then returns the complete mapping
        return self._shell_to_aoloc

    @property
    def full_ao_to_atom(self) -> torch.Tensor:
        # returns the full array mapping from atomic orbital index to the
        # atom location
        return self._ao_to_atom

    @property
    def full_ao_to_shell(self) -> torch.Tensor:
        # returns the full array mapping from atomic orbital index to the
        # shell location
        return self._ao_to_shell

    @property
    def ngauss_at_shell(self) -> List[int]:
        # returns the number of gaussian basis at the given shell
        return self._ngauss_at_shell_list

    @lru_cache(maxsize=32)
    def __len__(self) -> int:
        # total shells
        return self.shell_idxs[-1] - self.shell_idxs[0]

    @lru_cache(maxsize=32)
    def nao(self) -> int:
        # returns the number of atomic orbitals
        shell_idxs = self.shell_idxs
        return self.full_shell_to_aoloc[shell_idxs[-1]] - \
            self.full_shell_to_aoloc[shell_idxs[0]]

    @lru_cache(maxsize=32)
    def ao_idxs(self) -> Tuple[int, int]:
        # returns the lower and upper indices of the atomic orbitals of this object
        # in the full ao map (i.e. absolute indices)
        shell_idxs = self.shell_idxs
        return self.full_shell_to_aoloc[shell_idxs[0]], \
            self.full_shell_to_aoloc[shell_idxs[1]]

    @lru_cache(maxsize=32)
    def ao_to_atom(self) -> torch.Tensor:
        # get the relative mapping from atomic orbital relative index to the
        # absolute atom position
        # this is usually used in scatter in backward calculation
        return self.full_ao_to_atom[slice(*self.ao_idxs())]

    @lru_cache(maxsize=32)
    def ao_to_shell(self) -> torch.Tensor:
        # get the relative mapping from atomic orbital relative index to the
        # absolute shell position
        # this is usually used in scatter in backward calculation
        return self.full_ao_to_shell[slice(*self.ao_idxs())]

    def __getitem__(self, inp) -> LibcintWrapper:
        # get the subset of the shells, but keeping the environment and
        # parameters the same
        assert isinstance(inp, slice)
        assert inp.step is None or inp.step == 1
        assert inp.start is not None or inp.stop is not None

        # complete the slice
        nshells = self.shell_idxs[-1]
        if inp.start is None and inp.stop is not None:
            inp = slice(0, inp.stop)
        elif inp.start is not None and inp.stop is None:
            inp = slice(inp.start, nshells)

        # make the slice positive
        if inp.start < 0:
            inp = slice(inp.start + nshells, inp.stop)
        if inp.stop < 0:
            inp = slice(inp.start, inp.stop + nshells)

        return SubsetLibcintWrapper(self, inp)

    @lru_cache(maxsize=32)
    def get_uncontracted_wrapper(self) -> Tuple[LibcintWrapper, torch.Tensor]:
        # returns the uncontracted LibcintWrapper as well as the mapping from
        # uncontracted atomic orbital (relative index) to the relative index
        # of the atomic orbital
        new_atombases = []
        for atombasis in self.atombases:
            atomz = atombasis.atomz
            pos = atombasis.pos
            new_bases = []
            for shell in atombasis.bases:
                angmom = shell.angmom
                alphas = shell.alphas
                coeffs = shell.coeffs
                new_bases.extend([
                    CGTOBasis(angmom, alpha[None], coeff[None]) for (alpha, coeff) in zip(alphas, coeffs)
                ])
            new_atombases.append(AtomCGTOBasis(atomz=atomz, bases=new_bases, pos=pos))
        uncontr_wrapper = LibcintWrapper(
            new_atombases, spherical=self.spherical,
            basis_normalized=self.basis_normalized)

        # get the mapping uncontracted ao to the contracted ao
        uao2ao: List[int] = []
        idx_ao = 0
        # iterate over shells
        for i in range(len(self)):
            nao = self._nao_at_shell(i)
            uao2ao += list(range(idx_ao, idx_ao + nao)) * self.ngauss_at_shell[i]
            idx_ao += nao
        uao2ao_res = torch.tensor(uao2ao, dtype=torch.long, device=self.device)
        return uncontr_wrapper, uao2ao_res

    ############### misc functions ###############
    @contextmanager
    def centre_on_r(self, r: torch.Tensor) -> Iterator:
        # set the centre of coordinate to r (usually used in rinv integral)
        # r: (ndim,)
        try:
            env = self.atm_bas_env[-1]
            prev_centre = env[PTR_RINV_ORIG: PTR_RINV_ORIG + NDIM]
            env[PTR_RINV_ORIG: PTR_RINV_ORIG + NDIM] = r.detach().numpy()
            yield
        finally:
            env[PTR_RINV_ORIG: PTR_RINV_ORIG + NDIM] = prev_centre

    def _normalize_basis(self, basis_normalized: bool, alphas: torch.Tensor,
                         coeffs: torch.Tensor, angmom: int) -> torch.Tensor:
        # the normalization is obtained from CINTgto_norm from
        # libcint/src/misc.c, or
        # https://github.com/sunqm/libcint/blob/b8594f1d27c3dad9034984a2a5befb9d607d4932/src/misc.c#L80

        # if the basis has been normalized before, then just return the coeffs
        if basis_normalized:
            return coeffs

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

    def _nao_at_shell(self, sh: int) -> int:
        # returns the number of atomic orbital at the given shell index
        if self.spherical:
            op = CINT.CINTcgto_spheric
        else:
            op = CINT.CINTcgto_cart
        bas = self.atm_bas_env[1]
        return op(int2ctypes(sh), np2ctypes(bas))

class SubsetLibcintWrapper(LibcintWrapper):
    """
    A class to represent the subset of LibcintWrapper.
    If put into integrals or evaluations, this class will only evaluate
        the subset of the shells from its parent.
    The environment will still be the same as its parent.
    """
    def __init__(self, parent: LibcintWrapper, subset: slice):
        self._parent = parent
        self._shell_idxs = subset.start, subset.stop

    @property
    def shell_idxs(self) -> Tuple[int, int]:
        return self._shell_idxs

    @lru_cache(maxsize=32)
    def get_uncontracted_wrapper(self):
        # returns the uncontracted LibcintWrapper as well as the mapping from
        # uncontracted atomic orbital (relative index) to the relative index
        # of the atomic orbital of the contracted wrapper

        pu_wrapper, p_uao2ao = self._parent.get_uncontracted_wrapper()

        # determine the corresponding shell indices in the new uncontracted wrapper
        shell_idxs = self.shell_idxs
        gauss_idx0 = sum(self._parent.ngauss_at_shell[: shell_idxs[0]])
        gauss_idx1 = sum(self._parent.ngauss_at_shell[: shell_idxs[1]])
        u_wrapper = pu_wrapper[gauss_idx0: gauss_idx1]

        # construct the uao (relative index) mapping to the absolute index
        # of the atomic orbital in the contracted basis
        uao2ao = []
        idx_ao = 0
        for i in range(shell_idxs[0], shell_idxs[1]):
            nao = self._parent._nao_at_shell(i)
            uao2ao += list(range(idx_ao, idx_ao + nao)) * self._parent.ngauss_at_shell[i]
            idx_ao += nao
        uao2ao_res = torch.tensor(uao2ao, dtype=torch.long, device=self.device)
        return u_wrapper, uao2ao_res

    def __getitem__(self, inp):
        raise NotImplementedError("Indexing of SubsetLibcintWrapper is not implemented")

    def __getattr__(self, name):
        return getattr(self._parent, name)
