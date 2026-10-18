# Sources

What each source can do: discover records (`search`), download files (`fetch`), and
which checksum `fetch` verifies the bytes against. Rows marked "DataCite →" or
"OmicsDI →" are repositories reached through that index. The
[README](https://github.com/musharna/data-aggregator-mcp/blob/main/README.md) covers
install and what the server is for.

| Source                       | Discover |       Fetch       |     Checksum     |
| ---------------------------- | :------: | :---------------: | :--------------: |
| Zenodo                       |    ✅    |        ✅         |       md5        |
| DataCite → Figshare          |    ✅    |        ✅         |       md5        |
| DataCite → Dataverse         |    ✅    |        ✅         |       md5        |
| DataCite → OSF               |    ✅    |        ✅         |       md5        |
| DataCite → Dryad             |    ✅    |  manifest only¹   | sha-256 (listed) |
| DataCite → Mendeley & others |    ✅    |         —         |        —         |
| NCBI SRA                     |    ✅    |  ✅ (ENA FASTQ)   |       md5        |
| NCBI GEO                     |    ✅    |   ✅ (`suppl/`)   |      none²       |
| NCBI BioProject              |    ✅    |    → SRA links    |        —         |
| PubMed / OpenAIRE            |    ✅    | ✅ (OA full text) |      none³       |
| Hugging Face datasets        |    ✅    | ✅ (resolve URL)  |      none²       |
| DataONE (eco/env)            |    ✅    | ✅ (Member Node)  |  md5 / sha-256   |
| OmicsDI → PRIDE              |    ✅    |  ✅ (HTTPS FTP)   |      none²       |
| OmicsDI → MetaboLights       |    ✅    |  ✅ (HTTPS FTP)   |     sha-256      |
| OmicsDI → other MS repos     |    ✅    |         —         |        —         |
| DataCite → OpenNeuro         |    ✅    |   ✅ (snapshot)   |      none²       |
| DANDI (neurophysiology)      |    ✅    |    ✅ (302→S3)    |     sha-256      |
| CZ CELLxGENE (single-cell)   |    ✅    |   ✅ (H5AD/RDS)   |      none²       |
| OpenML (ML datasets)         |    ✅    |     ✅ (ARFF)     |       md5        |
| RCSB PDB (structures)        |    ✅    |  ✅ (.cif/.pdb)   |      none²       |
| UniProtKB (proteins)         |    ✅    |    ✅ (FASTA)     |      none²       |
| BioStudies (EBI)             |    ✅    | ✅ (study files)  |      none²       |
| GBIF (biodiversity)          |    ✅    | ✅ (Darwin Core)⁴ |      none²       |
| data.gov (DCAT-US)           |    ✅    |  ✅ (file URL)⁴   |      none³       |
| NASA CMR (Earth science)     |    ✅    |        —⁵         |        —         |
| GWAS Catalog                 |    ✅    |   → PMID bridge   |        —         |
| NGDC BioProject (China)      |    ✅    |  → GSA read sets  |        —         |
| Metabolomics Workbench       |    ✅    |         —         |        —         |
| MassIVE (mass spectrometry)  |    ✅    |         —         |        —         |
| Kaggle (ML datasets)         |    ✅    |         —         |        —         |

¹ Dryad downloads are token / bot-challenge gated, so `fetch` returns an error;
`resolve` still lists the files.

² No upstream checksum, so `fetch` does not verify these bytes. It still returns
an error on an HTTP error or when the download exceeds `max_bytes`.

³ No upstream checksum. Files declared as PDF or XML (literature full text, and
data.gov distributions with that mediaType) get an HTML sniff: an HTML login or
paywall page served in their place returns an error. Other files are not checked.

⁴ Only records that carry a downloadable file (a GBIF Darwin Core Archive, a
data.gov distribution URL); metadata-only records are discovery-only.

⁵ Discovery-only: granule downloads need an Earthdata login, which is not wired.
`resolve` returns the DOI and a data-access portal link.
