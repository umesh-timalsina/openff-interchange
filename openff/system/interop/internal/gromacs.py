from pathlib import Path
from typing import IO, TYPE_CHECKING, Dict, Union

import ele
import numpy as np
from openff.units import unit

from openff.system.components.misc import (
    _iterate_angles,
    _iterate_impropers,
    _iterate_propers,
    _store_bond_partners,
)
from openff.system.exceptions import UnsupportedExportError
from openff.system.models import TopologyKey

if TYPE_CHECKING:
    from openff.system.components.system import System


def to_gro(openff_sys: "System", file_path: Union[Path, str], decimal=8):
    """
    Write a .gro file. See
    https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#gro
    for more details, including the recommended C-style one-liners

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    if isinstance(file_path, str):
        path = Path(file_path)
    if isinstance(file_path, Path):
        path = file_path

    # Explicitly round here to avoid ambiguous things in string formatting
    rounded_positions = np.round(openff_sys.positions, decimal)
    rounded_positions = rounded_positions.to(unit.nanometer).magnitude

    n = decimal

    with open(path, "w") as gro:
        gro.write("Generated by OpenFF\n")
        gro.write(f"{openff_sys.positions.shape[0]}\n")
        typemap = _build_typemap(openff_sys)
        for atom in openff_sys.topology.mdtop.atoms:  # type: ignore
            res = atom.residue
            atom_name = typemap[atom.index]
            residue_idx = (res.index + 1) % 100000
            residue_name = res.name
            # TODO: Make sure these are in nanometers
            gro.write(
                f"%5d%-5s%5s%5d%{n+5}.{n}f%{n+5}.{n}f%{n+5}.{n}f\n"
                % (
                    residue_idx,
                    residue_name,
                    atom_name,
                    (atom.index + 1) % 100000,
                    rounded_positions[atom.index, 0],
                    rounded_positions[atom.index, 1],
                    rounded_positions[atom.index, 2],
                )
            )

        # TODO: Ensure nanometers
        box = openff_sys.box.to(unit.nanometer).magnitude  # type: ignore
        # Check for rectangular
        if (box == np.diag(np.diagonal(box))).all():
            for i in range(3):
                gro.write(f"{box[i, i]:11.7f}")
        else:
            for i in range(3):
                gro.write(f"{box[i, i]:11.7f}")
            for i in range(3):
                for j in range(3):
                    if i != j:
                        gro.write(f"{box[i, j]:11.7f}")

        gro.write("\n")


def to_top(openff_sys: "System", file_path: Union[Path, str]):
    """
    Write a .gro file. See
    https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#top
    for more details.

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    if isinstance(file_path, str):
        path = Path(file_path)
    if isinstance(file_path, Path):
        path = file_path

    with open(path, "w") as top_file:
        top_file.write("; Generated by OpenFF System\n")
        _write_top_defaults(openff_sys, top_file)
        typemap = _build_typemap(openff_sys)
        _write_atomtypes(openff_sys, top_file, typemap)
        # TODO: Write [ nonbond_params ] section

        # TODO: De-duplicate based on molecules
        # TODO: Handle special case of water
        _write_moleculetype(top_file)
        _write_atoms(top_file, openff_sys, typemap)
        _write_valence(top_file, openff_sys)
        _write_system(top_file, openff_sys)


def _write_top_defaults(openff_sys: "System", top_file: IO):
    """Write [ defaults ] section"""
    top_file.write("[ defaults ]\n")
    top_file.write("; nbfunc\tcomb-rule\tgen-pairs\tfudgeLJ\tfudgeQQ\n")

    if "vdW" in openff_sys.handlers:
        nbfunc = 1
        scale_lj = openff_sys["vdW"].scale_14
        gen_pairs = "yes"
    elif "Buckingham-6" in openff_sys.handlers:
        nbfunc = 2
        gen_pairs = "no"
        scale_lj = openff_sys["Buckingham-6"].scale_14

    top_file.write(
        "{:6d}\t{:6s}\t{:6s} {:8.6f} {:8.6f}\n\n".format(
            # self.system.nonbonded_function,
            # self.lookup_gromacs_combination_rules[self.system.combination_rule],
            # self.system.genpairs,
            # self.system.lj_correction,
            # self.system.coulomb_correction,
            nbfunc,
            str(2),
            gen_pairs,
            scale_lj,
            openff_sys.handlers["Electrostatics"].scale_14,  # type: ignore
        )
    )


def _build_typemap(openff_sys: "System") -> Dict:
    typemap = dict()
    elements: Dict[str, int] = dict()

    for atom in openff_sys.topology.mdtop.atoms:  # type: ignore[union-attr]
        element_symbol = atom.element.symbol
        # TODO: Use this key to condense, see parmed.openmm._process_nobonded
        # parameters = _get_lj_parameters([*parameters.values()])
        # key = tuple([*parameters.values()])

        if element_symbol not in elements.keys():
            elements[element_symbol] = 1
        else:
            elements[element_symbol] += 1

        atom_type = f"{element_symbol}{elements[element_symbol]}"
        typemap[atom.index] = atom_type

    return typemap


def _write_atomtypes(openff_sys: "System", top_file: IO, typemap: Dict):
    """Write [ atomtypes ] section"""

    if "vdW" in openff_sys.handlers:
        if "Buckingham-6" in openff_sys.handlers:
            raise UnsupportedExportError(
                "Cannot mix 12-6 and Buckingham potentials in GROMACS"
            )
        else:
            _write_atomtypes_lj(openff_sys, top_file, typemap)
    else:
        if "Buckingham-6" in openff_sys.handlers:
            _write_atomtypes_buck(openff_sys, top_file, typemap)
        else:
            raise UnsupportedExportError("No vdW interactions found")


def _write_atomtypes_lj(openff_sys: "System", top_file: IO, typemap: Dict):

    top_file.write("[ atomtypes ]\n")
    top_file.write(
        ";type, bondingtype, atomic_number, mass, charge, ptype, sigma, epsilon\n"
    )

    for atom_idx, atom_type in typemap.items():
        atom = openff_sys.topology.mdtop.atom(atom_idx)  # type: ignore[union-attr]
        mass = atom.element.mass
        atomic_number = atom.element.atomic_number
        parameters = _get_lj_parameters(openff_sys, atom_idx)
        sigma = parameters["sigma"].to(unit.nanometer).magnitude
        epsilon = parameters["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude
        top_file.write(
            "{:<11s} {:6d} {:.16g} {:.16g} {:5s} {:.16g} {:.16g}".format(
                atom_type,  # atom type
                # "XX",  # atom "bonding type", i.e. bond class
                atomic_number,
                mass,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                sigma,
                epsilon,
            )
        )
        top_file.write("\n")


def _write_atomtypes_buck(openff_sys: "System", top_file: IO, typemap: Dict):

    top_file.write("[ atomtypes ]\n")
    top_file.write(
        ";type, bondingtype, atomic_number, mass, charge, ptype, sigma, epsilon\n"
    )

    for atom_idx, atom_type in typemap.items():
        atom = openff_sys.topology.atom(atom_idx)  # type: ignore
        element = ele.element_from_atomic_number(atom.atomic_number)
        parameters = _get_buck_parameters(openff_sys, atom_idx)
        a = parameters["A"].to(unit.Unit("kilojoule / mol")).magnitude
        b = parameters["B"].to(1 / unit.nanometer).magnitude
        c = parameters["C"].to(unit.Unit("kilojoule / mol * nanometer ** 6")).magnitude

        top_file.write(
            "{:<11s} {:6d} {:.16g} {:.16g} {:5s} {:.16g} {:.16g} {:.16g}".format(
                atom_type,  # atom type
                # "XX",  # atom "bonding type", i.e. bond class
                atom.atomic_number,
                element.mass,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                a,
                b,
                c,
            )
        )
        top_file.write("\n")


def _write_moleculetype(top_file: IO):
    """Write the [ moleculetype ] section"""
    top_file.write("[ moleculetype ]\n")
    top_file.write("; Name\tnrexcl\n")
    top_file.write("MOL\t3\n\n")


def _write_atoms(
    top_file: IO,
    openff_sys: "System",
    typemap: Dict,
):
    """Write the [ atoms ] and [ pairs ] sections for a molecule"""
    top_file.write("[ atoms ]\n")
    top_file.write(";num, type, resnum, resname, atomname, cgnr, q, m\n")

    for atom in openff_sys.topology.mdtop.atoms:  # type: ignore
        atom_idx = atom.index
        mass = atom.element.mass
        atom_type = typemap[atom.index]
        res_idx = atom.residue.index
        res_name = str(atom.residue)
        top_key = TopologyKey(atom_indices=(atom_idx,))
        charge = (
            openff_sys.handlers["Electrostatics"].charges[top_key].magnitude  # type: ignore
        )
        top_file.write(
            "{:6d} {:18s} {:6d} {:8s} {:8s} {:6d} "
            "{:18.8f} {:18.8f}\n".format(
                atom_idx + 1,
                atom_type,
                res_idx + 1,
                res_name,
                atom_type,
                atom_idx + 1,
                charge,
                mass,
            )
        )

    top_file.write("[ pairs ]\n")
    top_file.write("; ai\taj\tfunct\n")

    _store_bond_partners(openff_sys.topology.mdtop)  # type: ignore[union-attr]

    for proper in _iterate_propers(openff_sys.topology.mdtop):  # type: ignore[union-attr]
        indices = tuple(a.index for a in proper)
        top_file.write(
            "{:7d} {:7d} {:6d}\n".format(
                indices[0] + 1,
                indices[3] + 1,
                1,
            )
        )


def _write_valence(
    top_file: IO,
    openff_sys: "System",
):
    """Write the [ bonds ], [ angles ], and [ dihedrals ] sections"""
    _write_bonds(top_file, openff_sys)
    _write_angles(top_file, openff_sys)
    _write_dihedrals(top_file, openff_sys)


def _write_bonds(top_file: IO, openff_sys: "System"):
    if "Bonds" not in openff_sys.handlers.keys():
        return

    top_file.write("[ bonds ]\n")
    top_file.write("; ai\taj\tfunc\tr\tk\n")

    bond_handler = openff_sys.handlers["Bonds"]

    for bond in openff_sys.topology.mdtop.bonds:  # type: ignore[union-attr]

        indices = tuple(sorted((bond.atom1.index, bond.atom2.index)))
        for top_key in bond_handler.slot_map:
            if top_key.atom_indices == indices:
                pot_key = bond_handler.slot_map[top_key]

        params = bond_handler.potentials[pot_key].parameters

        k = params["k"].m_as(unit.Unit("kilojoule / mole / nanometer ** 2"))
        length = params["length"].to(unit.nanometer).magnitude

        top_file.write(
            "{:7d} {:7d} {:4s} {:.16g} {:.16g}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                str(1),  # bond type (functional form)
                length,
                k,
            )
        )

        del pot_key

    top_file.write("\n\n")


def _write_angles(top_file: IO, openff_sys: "System"):
    if "Angles" not in openff_sys.handlers.keys():
        return

    _store_bond_partners(openff_sys.topology.mdtop)  # type: ignore[union-attr]

    top_file.write("[ angles ]\n")
    top_file.write("; ai\taj\tak\tfunc\tr\tk\n")

    angle_handler = openff_sys.handlers["Angles"]

    for angle in _iterate_angles(openff_sys.topology.mdtop):  # type: ignore[union-attr]
        indices = (
            angle[0].index,
            angle[1].index,
            angle[2].index,
        )
        for top_key in angle_handler.slot_map:
            if top_key.atom_indices == indices:
                pot_key = angle_handler.slot_map[top_key]

        params = angle_handler.potentials[pot_key].parameters
        k = params["k"].m_as(unit.Unit("kilojoule / mole / radian ** 2"))
        theta = params["angle"].to(unit.degree).magnitude

        top_file.write(
            "{:7d} {:7d} {:7d} {:4s} {:.16g} {:.16g}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                indices[2] + 1,  # atom k
                str(1),  # angle type (functional form)
                theta,
                k,
            )
        )

    top_file.write("\n\n")


def _write_dihedrals(top_file: IO, openff_sys: "System"):
    if "ProperTorsions" not in openff_sys.handlers:
        if "RBTorsions" not in openff_sys.handlers:
            if "ImproperTorsions" not in openff_sys.handlers:
                return

    _store_bond_partners(openff_sys.topology.mdtop)  # type: ignore[union-attr]

    top_file.write("[ dihedrals ]\n")
    top_file.write(";    i      j      k      l   func\n")

    rb_torsion_handler = openff_sys.handlers.get("RBTorsions", [])  # type: ignore
    proper_torsion_handler = openff_sys.handlers.get("ProperTorsions", [])  # type: ignore
    improper_torsion_handler = openff_sys.handlers.get("ImproperTorsions", [])  # type: ignore

    # TODO: Ensure number of torsions written matches what is expected
    for proper in _iterate_propers(openff_sys.topology.mdtop):  # type: ignore[union-attr]
        if proper_torsion_handler:
            for top_key in proper_torsion_handler.slot_map:  # type: ignore
                indices = tuple(a.index for a in proper)
                if top_key.atom_indices == indices:
                    pot_key = proper_torsion_handler.slot_map[top_key]  # type: ignore
                    params = proper_torsion_handler.potentials[pot_key].parameters  # type: ignore

                    k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                    periodicity = int(params["periodicity"])
                    phase = params["phase"].to(unit.degree).magnitude
                    idivf = int(params["idivf"]) if "idivf" in params else 1
                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} {:16g} {:16g} {:7d}\n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            1,
                            phase,
                            k / idivf,
                            periodicity,
                        )
                    )
        # This should be `if` if a single quartet can be subject to both proper and RB torsions
        if rb_torsion_handler:
            for top_key in rb_torsion_handler.slot_map:  # type: ignore
                indices = tuple(a.index for a in proper)
                if top_key.atom_indices == indices:
                    pot_key = rb_torsion_handler.slot_map[top_key]  # type: ignore
                    params = rb_torsion_handler.potentials[pot_key].parameters  # type: ignore

                    c0 = params["C0"].to(unit.Unit("kilojoule / mol")).magnitude
                    c1 = params["C1"].to(unit.Unit("kilojoule / mol")).magnitude
                    c2 = params["C2"].to(unit.Unit("kilojoule / mol")).magnitude
                    c3 = params["C3"].to(unit.Unit("kilojoule / mol")).magnitude
                    c4 = params["C4"].to(unit.Unit("kilojoule / mol")).magnitude
                    c5 = params["C5"].to(unit.Unit("kilojoule / mol")).magnitude

                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} "
                        "{:16g} {:16g} {:16g} {:16g} {:16g} {:16g} \n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            3,
                            c0,
                            c1,
                            c2,
                            c3,
                            c4,
                            c5,
                        )
                    )

    # TODO: Ensure number of torsions written matches what is expected
    for improper in _iterate_impropers(openff_sys.topology.mdtop):  # type: ignore
        if improper_torsion_handler:
            for top_key in improper_torsion_handler.slot_map:  # type: ignore
                indices = tuple(a.index for a in improper)
                if indices == top_key.atom_indices:
                    key = improper_torsion_handler.slot_map[top_key]  # type: ignore
                    params = improper_torsion_handler.potentials[key].parameters  # type: ignore

                    k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                    periodicity = int(params["periodicity"])
                    phase = params["phase"].to(unit.degree).magnitude
                    idivf = int(params["idivf"])
                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} {:.16g} {:.16g} {:.16g}\n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            4,
                            phase,
                            k / idivf,
                            periodicity,
                        )
                    )


def _write_system(top_file: IO, openff_sys: "System"):
    """Write the [ system ] section"""
    top_file.write("[ system ]\n")
    top_file.write("; name \n")
    top_file.write("System name\n\n")

    top_file.write("[ molecules ]\n")
    top_file.write("; Compound\tnmols\n")
    # TODO: Write molecules separately
    top_file.write("MOL\t1")

    top_file.write("\n")


def _get_lj_parameters(openff_sys: "System", atom_idx: int) -> Dict:
    vdw_hander = openff_sys.handlers["vdW"]
    atom_key = TopologyKey(atom_indices=(atom_idx,))
    identifier = vdw_hander.slot_map[atom_key]
    potential = vdw_hander.potentials[identifier]
    parameters = potential.parameters

    return parameters


def _get_buck_parameters(openff_sys: "System", atom_idx: int) -> Dict:
    buck_hander = openff_sys.handlers["Buckingham-6"]
    atom_key = TopologyKey(atom_indices=(atom_idx,))
    identifier = buck_hander.slot_map[atom_key]
    potential = buck_hander.potentials[identifier]
    parameters = potential.parameters

    return parameters
