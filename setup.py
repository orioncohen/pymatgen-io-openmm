from setuptools import setup, find_namespace_packages

import os

SETUP_PTH = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(SETUP_PTH, "README.md")) as f:
    desc = f.read()


setup(
    name="pymatgen-io-openmm",  # mypy: ignore
    packages=find_namespace_packages(include=["pymatgen.io.*"]),
    version="0.0.3",
    install_requires=[
        # "pymatgen>=2022.3.22",
        # "openmm",
        # "parmed",
        # "numpy",
        # "pytest",
        # "openbabel",
        # "rdkit",
        # "openff-toolkit>=0.12.0",
        # "packmol",
        # "openmmforcefields",
    ],
    extras_require={},
    package_data={},
    author="orion cohen",
    author_email="orion@lbl.gov",
    maintainer="orion cohen",
    url="https://github.com/orioncohen/pymatgen-io-openmm",
    license="BSD",
    description="A set of tools for setting up OpenMM simulations of battery systems.",
    long_description=desc,
    keywords=["pymatgen"],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)",
        "Operating System :: POSIX",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Topic :: Scientific/Engineering :: Chemistry",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
