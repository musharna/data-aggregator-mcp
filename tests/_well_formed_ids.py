"""A well-formed local id per resolve prefix, shared by the sweeps that drive every adapter.

A sweep that makes up its own ids (``x1``) breaks each time an adapter starts validating
its ids, and a sweep that only ever sends a rejected id tests nothing past the check.
Prefixes not listed here accept ``x1``.
"""

WELL_FORMED = {
    "biostudies": "S-EPMC1234567",
    "cellxgene": "af893e86-8e9f-41f1-a474-ef05359b1fb7",
    "dandi": "000004",
    "gbif": "6d27080f-ed47-48e2-90e8-cdebaba11a03",
    "gwas": "GCST000028",
    "hf": "owner/name",
    "omicsdi": "pride:PXD000001",
    "openml": "61",
    "pdb": "1ABC",
    "uniprot": "P12345",
    "zenodo": "77001",
}
