from itertools import product
import torch
import numpy as np
from ddft.basissets.cgto_basis import CGTOBasis
from ddft.hamiltons.hmolc0gauss import HamiltonMoleculeC0Gauss
from ddft.hamiltons.hmolcgauss import HamiltonMoleculeCGauss
from ddft.hamiltons.hatomygauss import HamiltonAtomYGauss
from ddft.hamiltons.hatomradial import HamiltonAtomRadial
from ddft.grids.radialgrid import LegendreRadialShiftExp
from ddft.grids.sphangulargrid import Lebedev
from ddft.grids.multiatomsgrid import BeckeMultiGrid

# Test procedures for checking the hamiltonian matrix's eigenvalues

dtype = torch.float64

def test_hamilton_molecule_c0_gauss():
    def runtest(atomz):
        # setup grid
        atompos = torch.tensor([[0.0, 0.0, 0.0]], dtype=dtype) # (natoms, ndim)
        atomzs = torch.tensor([atomz], dtype=dtype)
        radgrid = LegendreRadialShiftExp(1e-6, 1e3, 200, dtype=dtype)
        atomgrid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
        grid = BeckeMultiGrid(atomgrid, atompos, dtype=dtype)

        # setup basis
        nbasis = 60
        alphas = torch.logspace(np.log10(1e-4), np.log10(1e6), nbasis).unsqueeze(-1).to(dtype) # (nbasis, 1)
        centres = atompos.unsqueeze(1).repeat(nbasis, 1, 1)
        coeffs = torch.ones((nbasis, 1))
        h = HamiltonMoleculeC0Gauss(grid, alphas, centres, coeffs, atompos, atomzs, False).to(dtype)

        # compare the eigenvalues (no degeneracy because the basis is all radial)
        nevals = 5
        evals = get_evals(grid, h)[:nevals]
        true_evals = -0.5*atomz*atomz/(torch.arange(1, nevals+1).to(dtype)*1.0)**2
        print(evals - true_evals)
        assert torch.allclose(evals, true_evals)

    for atomz in [1.0,2.0]:
        runtest(atomz)


def test_hamilton_molecule_cartesian_gauss():
    def runtest(atomz, nelmts_tensor):
        # setup grid
        atompos = torch.tensor([[0.0, 0.0, 0.0]], dtype=dtype) # (natoms, ndim)
        atomzs = torch.tensor([atomz], dtype=dtype)
        radgrid = LegendreRadialShiftExp(1e-6, 1e3, 200, dtype=dtype)
        atomgrid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
        grid = BeckeMultiGrid(atomgrid, atompos, dtype=dtype)

        # setup basis
        nbasis = 60
        nelmts_val = 2
        if nelmts_tensor:
            nelmts = torch.ones(nbasis, dtype=torch.int32) * nelmts_val
        else:
            nelmts = nelmts_val
        alphas = torch.logspace(np.log10(1e-4), np.log10(1e6), nbasis*nelmts_val).to(dtype) # (nbasis,)
        centres = atompos.repeat(nbasis*nelmts_val, 1)
        coeffs = torch.ones((nbasis*nelmts_val,))
        ijks = torch.zeros((nbasis*nelmts_val, 3), dtype=torch.int32)
        h = HamiltonMoleculeCGauss(grid, ijks, alphas, centres, coeffs, nelmts, atompos, atomzs).to(dtype)

        # compare the eigenvalues (no degeneracy because the basis is all radial)
        nevals = 5
        evals = get_evals(grid, h)[:nevals]
        true_evals = -0.5*atomz*atomz/(torch.arange(1, nevals+1).to(dtype)*1.0)**2
        print(evals - true_evals)
        assert torch.allclose(evals, true_evals)

    for atomz, nelmts_tensor in product([1.0,2.0], [True, False]):
        runtest(atomz, nelmts_tensor)

def test_hamilton_molecule_cartesian_gauss1():
    def runtest(atomz):
        # setup grid
        atompos = torch.tensor([[0.0, 0.0, 0.0]], dtype=dtype) # (natoms, ndim)
        atomzs = torch.tensor([atomz], dtype=dtype)
        radgrid = LegendreRadialShiftExp(1e-6, 1e3, 200, dtype=dtype)
        atomgrid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
        grid = BeckeMultiGrid(atomgrid, atompos, dtype=dtype)

        # setup basis
        nbasis = 60
        nelmts = 1
        alphas = torch.logspace(np.log10(1e-4), np.log10(1e6), nbasis).repeat(4).to(dtype) # (4*nbasis,)
        centres = atompos.repeat(4*nbasis, 1)
        coeffs = torch.ones((4*nbasis,))
        ijks = torch.zeros((4,nbasis, 3), dtype=torch.int32)
        # L=1
        ijks[1,:,0] = 1
        ijks[2,:,1] = 1
        ijks[3,:,2] = 1
        ijks = ijks.view(4*nbasis, 3)
        h = HamiltonMoleculeCGauss(grid, ijks, alphas, centres, coeffs, nelmts, atompos, atomzs).to(dtype)

        # compare the eigenvalues (there is degeneracy in p-orbitals)
        nevals = 6
        evals = get_evals(grid, h)[:nevals]
        true_evals = -0.5*atomz*atomz/torch.tensor([1.0, 2.0, 2.0, 2.0, 2.0, 3.0]).to(dtype)**2
        print(evals - true_evals)
        assert torch.allclose(evals, true_evals)

    for atomz in [1.0,2.0]:
        runtest(atomz)

def test_hamilton_molecule_cgto():
    def runtest(atomz, basisname):
        # setup grid
        atompos = torch.tensor([[0.0, 0.0, 0.0]], dtype=dtype) # (natoms, ndim)
        atomzs = torch.tensor([atomz], dtype=dtype)
        radgrid = LegendreRadialShiftExp(1e-6, 1e3, 200, dtype=dtype)
        atomgrid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
        grid = BeckeMultiGrid(atomgrid, atompos, dtype=dtype)

        # setup basis
        basis = CGTOBasis(basisname)
        ijks, alphas, coeffs, nelmts, poss = basis.construct_basis(atomzs, atompos, cartesian=True)
        h = HamiltonMoleculeCGauss(grid, ijks, alphas, poss, coeffs, nelmts, atompos, atomzs).to(dtype)

        # compare the eigenvalues (there is degeneracy in p-orbitals)
        nevals = 1
        evals = get_evals(grid, h)[:nevals]
        true_evals = -0.5*atomz*atomz/torch.tensor([1.0]).to(dtype)**2
        print(evals - true_evals)
        assert torch.allclose(evals, true_evals, rtol=5e-2)

    for atomz in [1.0]:#,2.0]:
        runtest(atomz, "STO-6G")

def test_atom_gauss():
    def runtest(atomz, coulexp):
        # setup the grid and the basis
        gwidths = torch.logspace(np.log10(1e-6), np.log10(1e2), 60).to(dtype)
        radgrid = LegendreRadialShiftExp(1e-6, 1e3, 200, dtype=dtype)
        grid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
        h = HamiltonAtomYGauss(grid, gwidths, maxangmom=1).to(dtype)

        # obtain the eigenvalues
        nevals = 6
        atomzs = torch.tensor([atomz], dtype=dtype)
        charges = torch.tensor([0.0], dtype=dtype)
        evals = get_evals(grid, h, atomzs, charges)[:nevals]

        # taking into account the degeneracy
        true_evals = -0.5*atomz*atomz/torch.tensor([1.0, 2.0, 2.0, 2.0, 2.0, 3.0]).to(dtype)**2

        print(evals - true_evals)
        assert torch.allclose(evals, true_evals)

    for atomz, coulexp in product([1.0,2.0], [True,False]):
        runtest(atomz, coulexp)

def test_atom_radial_gauss():
    def runtest(atomz, coulexp):
        # setup the grid and the basis
        gwidths = torch.logspace(np.log10(1e-6), np.log10(1e3), 80).to(dtype)
        grid = LegendreRadialShiftExp(1e-7, 1e3, 200, dtype=dtype)
        h = HamiltonAtomRadial(grid, gwidths, coulexp=False).to(dtype)

        # obtain the eigenvalues
        nevals = 5
        atomzs = torch.tensor([atomz], dtype=dtype)
        charges = torch.tensor([0.0], dtype=dtype)
        evals = get_evals(grid, h, atomzs, charges)[:nevals]

        # taking into account the degeneracy
        true_evals = -0.5*atomz*atomz/torch.tensor([1.0, 2.0, 3.0, 4.0, 5.0]).to(dtype)**2

        print(evals - true_evals)
        assert torch.allclose(evals, true_evals)

    for atomz, coulexp in product([1.0,2.0], [True,False]):
        runtest(atomz, coulexp)

def get_evals(grid, h, *hparams):
    nr = grid.rgrid.shape[0]
    vext = torch.zeros(1, nr).to(dtype)
    H = h.fullmatrix(vext, *hparams)
    olp = h.overlap.fullmatrix()

    # check symmetricity of those matrices
    assert torch.allclose(olp-olp.transpose(-2,-1), torch.zeros_like(olp))
    assert torch.allclose(H-H.transpose(-2,-1), torch.zeros_like(H))

    mat = torch.solve(H[0], olp[0])[0]
    evals, evecs = torch.eig(mat)
    evals = torch.sort(evals.view(-1))[0]
    return evals
