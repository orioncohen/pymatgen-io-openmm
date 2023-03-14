"""
Utilities for implementing AlchemicalReactions
"""
import copy
from typing import List, Tuple, Dict, Optional
from io import StringIO
import pandas as pd
from MDAnalysis.lib.distances import capped_distance

from monty.json import MSONable

import numpy as np
import MDAnalysis as mda

from pymatgen.io.openmm.inputs import TopologyInput
from pymatgen.io.openmm.utils import (
    get_openff_topology,
    molgraph_from_openff_topology,
    molgraph_to_openff_topology,
)
from pymatgen.analysis.graphs import MoleculeGraph
import openff.toolkit as tk

from dataclasses import dataclass

import rdkit


def openff_counts_to_universe(openff_counts):
    """
    Quick conversion from a set of smiles to a MDanalysis Universe.

    Args:
        smiles: a dict of smiles: counts.

    Returns:
        A MDanalysis universe

    """
    topology = get_openff_topology(openff_counts).to_openmm()
    topology_input = TopologyInput(topology)
    with StringIO(topology_input.get_string()) as topology_file:
        universe = mda.Universe(topology_file, format="pdb")
    return universe


@dataclass
class HalfReaction(MSONable):
    """
    A HalfReaction that contains atoms that are created or deleted in a reaction.

    A HalfReaction are only useful within the context of a specific ReactiveSystem

    Args:
        create_bonds: a list of atoms that form new bonds. These should be paired
            with another half reactions with a corresponding set of atoms.
        delete_bonds: a list of tuples of atom indices to delete bonds between.
        delete_atoms: a list of atom indices to delete.
    """

    create_bonds: List[int]
    delete_bonds: List[Tuple[int, int]]
    delete_atoms: List[int]

    def remap(self, old_to_new_map: Dict[int, int]) -> "HalfReaction":
        """
        A pure function that creates a new half reaction with a different mapping

        Args:
            old_to_new_map: a mapping between atom indices

        Returns:

        """
        return HalfReaction(
            create_bonds=[old_to_new_map[i] for i in self.create_bonds],
            delete_bonds=[
                (old_to_new_map[i], old_to_new_map[j]) for i, j in self.delete_bonds
            ],
            delete_atoms=[old_to_new_map[i] for i in self.delete_atoms],
        )


@dataclass
class ReactiveAtoms(MSONable):
    """
    A ReactiveAtoms object that contains all the atoms that participate in a reaction.

    ReactiveAtoms are only useful within the context of a specific ReactiveSystem

    Args:
        half_reactions: a dictionary of half reactions where the key is the trigger atom
            and the value is the HalfReaction
        trigger_atoms_left: a list of trigger atoms for the "left" side of the reaction
        trigger_atoms_right: a list of trigger atoms for the "right" side of the reaction
    """

    half_reactions: Dict[int, HalfReaction]
    trigger_atoms_left: List[int]
    trigger_atoms_right: List[int]
    barrier: float = 0.0

    def remap(self, old_to_new_map: Dict[int, int]) -> "ReactiveAtoms":
        """
        A pure function that creates a new ReactiveAtoms with a different mapping

        Args:
            old_to_new_map: a mapping between atom indices

        Returns:

        """
        return ReactiveAtoms(
            half_reactions={
                old_to_new_map[k]: v.remap(old_to_new_map)
                for k, v in self.half_reactions.items()
            },
            trigger_atoms_left=[old_to_new_map[i] for i in self.trigger_atoms_left],
            trigger_atoms_right=[old_to_new_map[i] for i in self.trigger_atoms_right],
            barrier=self.barrier,
        )


class AlchemicalReaction(MSONable):
    """
    An AlchemicalReaction for use in a OpenmmAlchemyGen generator.
    """

    def __init__(
        self,
        name: str = "alchemical reaction",
        select_dict: Dict[str, str] = None,
        create_bonds: List[Tuple[str, str]] = None,
        delete_bonds: List[Tuple[str, str]] = None,
        delete_atoms: List[str] = None,
        barrier: float = 0.0,
    ):
        """
        Args:
            name: a name for the reaction
            select_dict: a dictionary of atom selection strings
            create_bonds: a list of tuples of atom selection strings to create bonds between
            delete_bonds: a list of tuples of atom selection strings to delete bonds between
            delete_atoms: a list of atom selection strings to delete
        """
        self.name = name
        self.select_dict = select_dict or {}
        self.create_bonds = create_bonds or []
        self.delete_bonds = delete_bonds or []
        self.delete_atoms = delete_atoms or []
        self.barrier = barrier

    # TODO: make sure things dont break if there are multiple possible reactions
    # TODO: need to make sure that we won't get an error if something reacts with itself

    @staticmethod
    def _build_reactive_atoms_df(
        universe, select_dict, create_bonds, delete_bonds, delete_atoms
    ):
        """
        This function builds a dataframe contains all the atoms that participate in the alchemical
        reaction. For each atom, it includes their atom index, which reaction they participate in,
        and the type of reaction it is.

        Args:
            universe:
            select_dict:
            create_bonds:
            delete_bonds:
            delete_atoms:

        Returns:

        """
        participating_atoms = []
        # loop to reuse code for create_bonds and delete_bonds
        for rxn_type, bonds in {
            "create_bonds": create_bonds,
            "delete_bonds": delete_bonds,
        }.items():
            # loop through every unique bond type
            for bond_n, bond in enumerate(bonds):
                atoms_ix_0 = universe.select_atoms(select_dict[bond[0]]).ix
                atoms_ix_1 = universe.select_atoms(select_dict[bond[1]]).ix
                # loop through each type of atom in bond
                for half_rxn_ix, atom_ix_list in enumerate([atoms_ix_0, atoms_ix_1]):
                    # loop through each unique atom of that type
                    for atom_ix in atom_ix_list:
                        res_ix = universe.atoms[atom_ix].residue.ix
                        participating_atoms.append(
                            {
                                "atom_ix": atom_ix,
                                "res_ix": res_ix,
                                "type": rxn_type,
                                "bond_n": bond_n,
                                "half_rxn_ix": half_rxn_ix,
                            }
                        )
        # loop through atoms of each type
        for atoms in delete_atoms:
            atom_ix_array = universe.select_atoms(select_dict[atoms]).ix
            # loop through each unique atom of that type
            for atom_ix in atom_ix_array:
                res_ix = universe.atoms[atom_ix].residue.ix
                participating_atoms.append(
                    {
                        "atom_ix": atom_ix,
                        "res_ix": res_ix,
                        "type": "delete_atom",
                        "bond_n": np.nan,
                        "half_rxn_ix": np.nan,
                    }
                )
        df = pd.DataFrame(participating_atoms)
        df = df.astype({"bond_n": "Int64", "half_rxn_ix": "Int64"})
        return df

    @staticmethod
    def _add_trigger_atoms(df, universe):
        """
        This extracts all of the "trigger" atoms that instigate a specific alchemical reaction,
        e.g. the atoms that can move within some cutoff to react with each other. It then organizes
        all atoms by which trigger atoms then correspond to.

        Args:
            df:
            universe:

        Returns:

        """
        trigger_atom_ix = df[((df.type == "create_bonds") & (df.bond_n == 0))][
            "atom_ix"
        ]
        trigger_atom_dfs = []
        # pair each trigger atom with its associated create_bonds, delete_bonds and delete_atoms
        for ix in trigger_atom_ix:
            # TODO: should this be one or two bonds? (bonded bonded index {ix})
            within_one_bond = f"(index {ix}) or (bonded index {ix})"
            nearby_atoms_ix = universe.select_atoms(within_one_bond).ix
            atoms_df = df[np.isin(df["atom_ix"], nearby_atoms_ix)]
            atoms_df["trigger_ix"] = ix
            trigger_atom_dfs.append(atoms_df)
        return pd.concat(trigger_atom_dfs)

    @staticmethod
    def _mini_universe_reactive_atoms_df(
        openff_mols, select_dict, create_bonds, delete_bonds, delete_atoms
    ):
        # we first create a small universe with one copy of each residue
        openff_singles = {mol: 1 for mol in openff_mols}
        universe_mini = openff_counts_to_universe(openff_singles)

        # next we find the reactive atoms in the small universe
        atoms_mini_df = AlchemicalReaction._build_reactive_atoms_df(
            universe_mini, select_dict, create_bonds, delete_bonds, delete_atoms
        )

        # then we assign trigger atoms to each reactive atom based on distance
        atoms_w_triggers_mini_df = AlchemicalReaction._add_trigger_atoms(
            atoms_mini_df, universe_mini
        )
        return atoms_w_triggers_mini_df

    @staticmethod
    def _expand_to_all_atoms(trig_df, res_sizes, res_counts):
        """
        All previous functionality only operated on a small universe with one copy of each
        residue, ultimately returning a small dataframe representing a subset of all
        residues. This function expands that dataframe to encapsulate the whole simulation.
        Since we know how many of each residue are present and how many atoms each residue
        has, we can essentially concatenate many duplicates of the small dataframe and then
        increment the atom indexes to reflect the full simulation.

        Args:
            trig_df:
            res_sizes:
            res_counts:

        Returns:

        """
        trig_df = trig_df.sort_values("res_ix")
        # create a list of offsets to be applied to the duplicated dataframe
        res_offsets = np.cumsum(np.array(res_sizes) * (np.array(res_counts) - 1))
        res_offsets = np.insert(res_offsets, 0, 0)
        # duplicate the small dataframe into a larger dataframe
        big_df_list = []
        for res_ix, res_df in trig_df.groupby(["res_ix"]):
            expanded_df = pd.concat([res_df] * res_counts[res_ix])
            n_atoms = len(res_df)
            # create and apply offsets
            offsets = np.arange(
                res_offsets[res_ix], res_offsets[res_ix + 1] + 1, res_sizes[res_ix]
            )
            offset_array = np.repeat(offsets, n_atoms)
            expanded_df.atom_ix += offset_array
            expanded_df.trigger_ix += offset_array
            big_df_list += [expanded_df]
        big_df = pd.concat(big_df_list)
        return big_df

    @staticmethod
    def _build_half_reactions_dict(all_atoms_df) -> Dict[int, HalfReaction]:
        """
        This takes the dataframe of all atoms and turns it into an easily parsable dictionary.
        Each "trigger atom" has a set of atom and bond deletions that are triggered when a new
        bond is created.

        Args:
            all_atoms_df:

        Returns:

        """
        half_reactions = {}
        for trigger_ix, atoms_df in all_atoms_df.groupby(["trigger_ix"]):
            trigger_ix = int(trigger_ix)
            create_ix = list(
                atoms_df[atoms_df.type == "create_bonds"].sort_values("bond_n")[
                    "atom_ix"
                ]
            )
            delete_ix = atoms_df[atoms_df.type == "delete_bonds"]
            unique_bond_n = delete_ix["bond_n"].unique()
            delete_ix = [
                tuple(delete_ix[delete_ix["bond_n"] == bond_n]["atom_ix"].values)
                for bond_n in unique_bond_n
            ]
            delete_atom_ix = list(
                atoms_df[atoms_df.type == "delete_atom"]["atom_ix"].values
            )
            half_reaction = HalfReaction(
                create_bonds=create_ix,
                delete_bonds=delete_ix,
                delete_atoms=delete_atom_ix,
            )
            half_reactions[trigger_ix] = half_reaction
        return half_reactions

    @staticmethod
    def _get_triggers(all_atoms_df):
        """This returns the trigger atoms for each half reaction."""
        trigger_atoms_left = all_atoms_df[
            (all_atoms_df.type == "create_bonds")
            & (all_atoms_df.bond_n == 0)
            & (all_atoms_df.half_rxn_ix == 0)
        ]["trigger_ix"].values

        trigger_atoms_right = all_atoms_df[
            (all_atoms_df.type == "create_bonds")
            & (all_atoms_df.bond_n == 0)
            & (all_atoms_df.half_rxn_ix == 1)
        ]["trigger_ix"].values

        return list(trigger_atoms_left), list(trigger_atoms_right)

    def make_reactive_atoms(
        self,
        openff_counts,
    ):
        """
        This strings together several other utility functions to return the full half reactions dictionary.
        Optional return arguments are provided to allow for the trigger atom indices to be extracted.

        Args:
            openff_counts:

        Returns:

        """
        # create a dataframe with reactive atoms for a small universe with one copy of each residue
        atoms_w_triggers_mini_df = AlchemicalReaction._mini_universe_reactive_atoms_df(
            openff_counts.keys(),
            self.select_dict,
            self.create_bonds,
            self.delete_bonds,
            self.delete_atoms,
        )

        # finally we expand the dataframe to include all atoms in the system
        res_sizes = [mol.n_atoms for mol in openff_counts.keys()]
        res_counts = list(openff_counts.values())
        atoms_w_triggers_df = AlchemicalReaction._expand_to_all_atoms(
            atoms_w_triggers_mini_df, res_sizes, res_counts
        )

        # we can now extract the trigger atoms from the dataframe
        triggers_left, triggers_right = AlchemicalReaction._get_triggers(
            atoms_w_triggers_df
        )

        # convert the half reaction df into a dictionary
        half_reactions_dict = AlchemicalReaction._build_half_reactions_dict(
            atoms_w_triggers_df
        )

        # and return
        return ReactiveAtoms(
            half_reactions=half_reactions_dict,
            trigger_atoms_left=triggers_left,
            trigger_atoms_right=triggers_right,
            barrier=self.barrier,
        )

    def visualize_reactions(
        self,
        openff_mols: List[tk.Molecule],
        filename: Optional[str] = None,
    ) -> rdkit.Chem.rdchem.Mol:
        from rdkit.Chem import rdCoordGen
        from rdkit.Chem.Draw import rdMolDraw2D

        # create a dataframe with reactive atoms for a small universe with one copy of each residue
        atoms_w_triggers_mini_df = AlchemicalReaction._mini_universe_reactive_atoms_df(
            openff_mols,
            self.select_dict,
            self.create_bonds,
            self.delete_bonds,
            self.delete_atoms,
        )
        half_reactions_dict = AlchemicalReaction._build_half_reactions_dict(
            atoms_w_triggers_mini_df
        )
        # we can now extract the trigger atoms from the dataframe
        triggers_left, triggers_right = AlchemicalReaction._get_triggers(
            atoms_w_triggers_mini_df
        )
        u = openff_counts_to_universe({mol: 1 for mol in openff_mols})
        rdmol = u.atoms.convert_to("RDKIT")
        rdCoordGen.AddCoords(rdmol)
        for trigger_atom, half_reaction in half_reactions_dict.items():
            for i, bond in enumerate(half_reaction.create_bonds):
                l_r = "L" if trigger_atom in triggers_left else "R"
                rdmol.GetAtomWithIdx(bond).SetProp(
                    "atomNote", f"{trigger_atom}: form {l_r}{i}"
                )
            for l_atom, r_atom in half_reaction.delete_bonds:
                rdmol.GetBondBetweenAtoms(l_atom, r_atom).SetProp(
                    "bondNote", f"{trigger_atom}: del bond"
                )
            for atom in half_reaction.delete_atoms:
                rdmol.GetAtomWithIdx(atom).SetProp(
                    "atomNote", f"{trigger_atom}: del atom"
                )

        rdCoordGen.AddCoords(rdmol)
        if filename:
            # Draw.MolToFile(rdmol, size=(1500, 1500), filename=filename)
            d = rdMolDraw2D.MolDraw2DCairo(1500, 1500)
            left_colors = {int(atom): (0.95, 0.8, 0.8) for atom in triggers_left}
            right_colors = {int(atom): (0.8, 0.9, 0.95) for atom in triggers_right}
            rdMolDraw2D.PrepareAndDrawMolecule(
                d,
                rdmol,
                highlightAtoms=[int(atom) for atom in triggers_left + triggers_right],
                highlightAtomColors={**left_colors, **right_colors},
            )
            d.FinishDrawing()
            d.WriteDrawingText(filename)

        return rdmol


class ReactiveSystem(MSONable):
    def __init__(
        self,
        reactive_atom_sets: List[ReactiveAtoms],
        molgraph: MoleculeGraph,
    ):
        self.reactive_atom_sets = reactive_atom_sets
        self.molgraph = molgraph

    @staticmethod
    def from_reactions(
        openff_counts: Dict[tk.Molecule, int],
        alchemical_reactions: List[AlchemicalReaction],
    ):
        # calculate reactive atoms
        reactive_atoms = [
            reaction.make_reactive_atoms(openff_counts)
            for reaction in alchemical_reactions
        ]

        # build a corresponding molgraph
        topology = get_openff_topology(openff_counts)
        molgraph = molgraph_from_openff_topology(topology)
        # molgraph_to_rxn_index = {i: i for i in range(topology.n_atoms)}

        return ReactiveSystem(
            reactive_atom_sets=reactive_atoms,
            molgraph=molgraph,
        )

    @staticmethod
    def _sample_reactions(
        reactive_atoms: ReactiveAtoms,
        positions: np.ndarray,
        reaction_temperature: float,
        distance_cutoff: float,
    ) -> List[Tuple[HalfReaction, HalfReaction]]:
        """


        Args:
            reactive_atoms:
            positions:
            reaction_temperature:
            distance_cutoff:

        Returns:

        """
        # get the positions of the trigger atoms
        atoms_left = positions[reactive_atoms.trigger_atoms_left]
        atoms_right = positions[reactive_atoms.trigger_atoms_right]

        # calculate distances between trigger atoms
        reaction_pairs = capped_distance(
            atoms_left, atoms_right, distance_cutoff, return_distances=False
        )

        reactions = []
        reacted_atoms = []
        for l, r in reaction_pairs:

            # calculate the probability of the reaction
            p = np.exp(-reactive_atoms.barrier / reaction_temperature)

            # don't react the same atom twice
            if l in reacted_atoms or r in reacted_atoms:
                p = 0

            if np.random.random() < p:
                reactions.append(
                    (
                        reactive_atoms.half_reactions[l],
                        reactive_atoms.half_reactions[r],
                    )
                )
                reacted_atoms += [l, r]
        return reactions

    @staticmethod
    def _react_molgraph(molgraph, old_to_new_map, full_reactions):
        # TODO: this copy should be removed once things are working?
        molgraph = copy.deepcopy(molgraph)

        for left_reaction, right_reaction in full_reactions:

            # delete bonds
            for l_atom_ix, r_atom_ix in (
                left_reaction.delete_bonds + right_reaction.delete_bonds
            ):
                molgraph.break_edge(l_atom_ix, r_atom_ix, allow_reverse=True)

            # create bonds
            assert len(left_reaction.create_bonds) == len(right_reaction.create_bonds)
            for bond_ix in range(len(left_reaction.create_bonds)):
                molgraph.add_edge(
                    left_reaction.create_bonds[bond_ix],
                    right_reaction.create_bonds[bond_ix],
                )

            # delete atoms
            deleted_atoms = []
            for atom_ix in sorted(
                left_reaction.delete_atoms + right_reaction.delete_atoms
            ):
                molgraph.remove_node(atom_ix - len(deleted_atoms))
                deleted_atoms.append(atom_ix)

            # update the molgraph_to_rxn_index
            assert list(old_to_new_map.keys()) == list(
                range(len(old_to_new_map))
            )  # TODO: remove
            new_to_old_index = list(old_to_new_map.keys())
            for atom_ix in deleted_atoms[::-1]:
                new_to_old_index.pop(atom_ix)
            # this works because only deleting atoms means keys() are a contiguous list
            # so keys are always a contiguous list of indices

            old_to_new_map = {old: new for new, old in enumerate(new_to_old_index)}

        return molgraph, old_to_new_map

    def react(self, positions, reaction_temperature=1, distance_cutoff=4):
        """
        Reacts the system with the given positions.
        """
        # we use new to old map because it can be maintained as a list of indices
        old_to_new_map = {i: i for i in range(len(self.molgraph))}
        molgraph = self.molgraph
        for i, reactive_atoms in enumerate(self.reactive_atom_sets):
            # remapping is only needed if the molgraph has changed, e.g. atoms deleted
            if len(old_to_new_map) != len(self.molgraph):
                reactive_atoms = reactive_atoms.remap(old_to_new_map)

            # sample reactions
            full_reactions = ReactiveSystem._sample_reactions(
                reactive_atoms,
                positions,
                reaction_temperature,
                distance_cutoff,
            )

            # react our molgraph, deleting atoms may create a different index_map
            molgraph, old_to_new_map = ReactiveSystem._react_molgraph(
                molgraph,
                old_to_new_map,
                full_reactions,
            )
        # only needed if atom indices have changed
        if len(old_to_new_map) != len(self.molgraph):
            self.reactive_atom_sets = [
                atoms.remap(old_to_new_map) for atoms in self.reactive_atom_sets
            ]
        self.molgraph = molgraph

    def generate_topology(self, update_self=False) -> tk.Topology:
        topology, new_to_old_index = molgraph_to_openff_topology(
            self.molgraph, return_index_map=True
        )
        old_to_new_index = {v: k for k, v in new_to_old_index.items()}

        if update_self:
            self.molgraph = molgraph_from_openff_topology(topology)
            self.reactive_atom_sets = [
                atoms.remap(old_to_new_index) for atoms in self.reactive_atom_sets
            ]
        return topology
