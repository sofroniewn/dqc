import torch
from torch.autograd import gradcheck, gradgradcheck
from ddft.eks.base_eks import BaseEKS
from ddft.eks.hartree import Hartree
from ddft.basissets.cartesian_cgto import CartCGTOBasis
from ddft.grids.radialgrid import LegendreShiftExpRadGrid, LegendreLogM3RadGrid
from ddft.grids.sphangulargrid import Lebedev
from ddft.grids.multiatomsgrid import BeckeMultiGrid
from ddft.utils.safeops import safepow

"""
Test the gradient for the intermediate methods (not basic module and not API)
"""

dtype = torch.float64
device = torch.device("cpu")

def test_grad_basis_cgto():
    basisname = "6-311++G**"
    ns0 = 7

    rmin = 1e-5
    rmax = 1e2
    nr = 100
    prec = 13
    nrgrid = 148 * nr

    def fcn(atomzs, atomposs, wf, vext):
        radgrid = LegendreShiftExpRadGrid(nr, rmin, rmax, dtype=dtype)
        sphgrid = Lebedev(radgrid, prec=prec, basis_maxangmom=4, dtype=dtype)
        grid = BeckeMultiGrid(sphgrid, atomposs, dtype=dtype)
        bases_list = [CartCGTOBasis(atomz, basisname, dtype=dtype) for atomz in atomzs]
        h = bases_list[0].construct_hamiltonian(grid, bases_list, atomposs)
        h.set_basis(gradlevel=0)
        H_model = h.get_vext(vext) + h.get_kincoul()
        y = H_model.mm(wf)
        return (y**2).sum()

    atomzs = torch.tensor([1.0, 1.0], dtype=dtype)
    atomposs = torch.tensor([[-0.5, 0.0, 0.0], [0.5, 0.0, 0.0]], dtype=dtype).requires_grad_()
    ns = ns0 * len(atomzs)
    wf = torch.ones((1,ns,1), dtype=dtype)
    vext = torch.zeros((1,nrgrid), dtype=dtype)

    gradcheck(fcn, (atomzs, atomposs, wf, vext))
    gradgradcheck(fcn, (atomzs, atomposs, wf, vext))

def test_grad_poisson_radial():
    ra = torch.tensor(2., dtype=dtype, device=device).requires_grad_()
    w = torch.linspace(0.8, 1.2, 5, dtype=dtype, device=device).unsqueeze(-1).requires_grad_() # (nw,1)

    gridparams = (ra,)
    def getgrid(ra):
        radgrid = LegendreLogM3RadGrid(nr=100, ra=ra, dtype=dtype, device=device)
        return radgrid

    runtest_grad_poisson(w, getgrid, gridparams)

def test_grad_poisson_spherical():
    ra = torch.tensor(2., dtype=dtype, device=device).requires_grad_()
    w = torch.linspace(0.8, 1.2, 5, dtype=dtype, device=device).unsqueeze(-1).requires_grad_() # (nw,1)

    gridparams = (ra,)
    def getgrid(ra):
        radgrid = LegendreLogM3RadGrid(nr=100, ra=ra, dtype=dtype, device=device)
        grid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype, device=device)
        return grid

    runtest_grad_poisson(w, getgrid, gridparams)

def test_grad_poisson_multiatoms():
    ra = torch.tensor(2., dtype=dtype, device=device).requires_grad_()
    w = torch.linspace(0.8, 1.2, 5, dtype=dtype, device=device).unsqueeze(-1).requires_grad_() # (nw,1)
    dist = torch.tensor(1., dtype=dtype, device=device).requires_grad_()

    gridparams = (ra, dist)
    def getgrid(ra, dist):
        atompos = torch.tensor([[-0.5, 0., 0.], [0.5, 0., 0.]], dtype=dtype, device=device) * dist
        radgrid = LegendreLogM3RadGrid(nr=100, ra=ra, dtype=dtype, device=device)
        sphgrid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype, device=device)
        grid = BeckeMultiGrid(sphgrid, atompos=atompos, dtype=dtype, device=device)
        return grid

    def getr(grid):
        return grid.rgrid.norm(dim=-1)

    # turn off the res check because the results won't be accurate enough
    runtest_grad_poisson(w, getgrid, gridparams, getr, reschk=False)


def runtest_grad_poisson(w, getgrid, gridparams, getr=None, reschk=True, gradchk=True):
    if getr is None:
        getr = lambda grid: grid.rgrid[:,0]

    def getfpois(w, *gridparams):
        grid = getgrid(*gridparams)
        r = getr(grid) # (nr)
        f = torch.exp(-r/w) # (nw, nr)
        fpois = grid.solve_poisson(f) # (nw, nr)
        return fpois

    def getloss(w, *gridparams):
        fpois = getfpois(w, *gridparams)
        loss = fpois.mean(dim=-1).sum()
        return loss

    grid = getgrid(*gridparams)
    r = getr(grid) # (nr)
    f = torch.exp(-r/w)

    fpois = getfpois(w, *gridparams)
    fpois_true = w*w*f + 2*w*w*w/r*torch.expm1(-r/w) # (nw, nr)
    if reschk:
        assert torch.allclose(fpois, fpois_true)

    # analytically calculated gradients
    gwidth_true = 4*w*f.mean(dim=-1, keepdim=True) + (f*r).mean(dim=-1, keepdim=True) +\
        6*w*w*(torch.expm1(-r/w)/r).mean(dim=-1, keepdim=True) # (nw,1)

    loss = getloss(w, *gridparams)
    gwidth, = torch.autograd.grad(loss, (w,), retain_graph=True)
    if reschk:
        assert torch.allclose(gwidth, gwidth_true)

    if gradchk:
        gradcheck(getloss, (w, *gridparams))
        gradgradcheck(getloss, (w, *gridparams))
