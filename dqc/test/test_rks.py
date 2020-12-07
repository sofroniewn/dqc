import torch
import pytest
from dqc.qccalc.rks import RKS
from dqc.system.mol import Mol

dtype = torch.float64

@pytest.mark.parametrize(
    "xc,moldesc,energy_true",
    [
        ("lda,", "H -0.5 0 0; H 0.5 0 0", -0.979143260),  # pyscf: -0.979143262
        ("lda,", "Li -2.5 0 0; Li 2.5 0 0", -14.3927863482007),  # pyscf: -14.3927863482007
        ("lda,", "N -1 0 0; N 1 0 0", -107.726124017789),  # pyscf: -107.726124017789
        ("lda,", "F -1.25 0 0; F 1.25 0 0", -197.005308558326),  # pyscf: -197.005308558326
        ("lda,", "C -1 0 0; O 1 0 0", -111.490687028797),  # pyscf: -111.490687028797
    ]
)
def test_rks_energy(xc, moldesc, energy_true):
    mol = Mol(moldesc, basis="6-311++G**", dtype=dtype)
    qc = RKS(mol, xc=xc).run()
    ene = qc.energy()
    assert torch.allclose(ene, ene * 0 + energy_true)

@pytest.mark.parametrize(
    "xc,atomzs,poss",
    [
        ("lda,", [1, 1], [[-0.5, 0.0, 0.0], [0.5, 0.0, 0.0]]),
        ("lda,", [3, 3], [[-2.5, 0.0, 0.0], [2.5, 0.0, 0.0]]),
        ("lda,", [7, 7], [[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
        ("lda,", [9, 9], [[-1.25, 0.0, 0.0], [1.25, 0.0, 0.0]]),
        ("lda,", [6, 8], [[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
    ]
)
def test_rks_grad_pos(xc, atomzs, poss):
    def get_energy(poss_tensor):
        mol = Mol((atomzs, poss_tensor), basis="3-21G", dtype=dtype)
        qc = RKS(mol, xc=xc).run()
        return qc.energy()
    poss_tensor = torch.tensor(poss, dtype=dtype, requires_grad=True)
    torch.autograd.gradcheck(get_energy, (poss_tensor,))

if __name__ == "__main__":
    import time
    xc = "lda,"
    basis = "6-311++G**"
    poss = torch.tensor([[0.0, 0.0, 0.5], [0.0, 0.0, -0.5]], dtype=dtype).requires_grad_()
    moldesc = ([3, 3], poss)
    mol = Mol(moldesc, basis=basis, dtype=dtype)
    # mol = Mol("Li -2.5 0 0; Li 2.5 0 0", basis="6-311++G**", dtype=dtype)
    # mol = Mol("H -0.5 0 0; H 0.5 0 0", basis=basis, dtype=dtype)
    t0 = time.time()
    qc = RKS(mol, xc=xc).run()
    ene = qc.energy()
    t1 = time.time()
    print(ene)
    print("Forward time : %fs" % (t1 - t0))

    dedposs = torch.autograd.grad(ene, poss)
    t2 = time.time()
    print(dedposs)
    print("Backward time: %fs" % (t2 - t1))