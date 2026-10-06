# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `resolve` on a paper (`pubmed:` or `openaire:`) adds the data deposits Europe PMC
  text-mines from it, as `references` links: BioProject, NGDC, GEO, SRA/ENA reads,
  ArrayExpress (`biostudies:`) and GWAS Catalog ids, and bare ProteomeXchange and
  MetaboLights accessions. Most deposits are named in a paper and linked nowhere else:
  the BioProjects of Boothby 2017 (tardigrade desiccation) and of the snow leopard
  virome paper have no PubMed link either way, and now resolve from the paper. A paper
  names others' data too, so a `references` link is a lead, not proof of authorship; a
  failed lookup is named in `errors.links`.

## [0.65.0] - 2026-10-06

### Added

- `ngdc`, a discovery-only source for NGDC (China National Center for Bioinformation):
  BioProjects deposited in China (`ngdc:PRJCA…`), with the Genome Sequence Archive read
  sets (CRA) each holds and its landing page. NCBI does not mirror them, and in the
  round-2 benchmark five keyed studies across three tasks were held only there; no arm
  found any, web search included. NGDC's index also mirrors INSDC projects (116 of 124
  for "axolotl"), which the `omics` source already covers, so only NGDC's own are asked
  for. The API is the undocumented JSON endpoint behind NGDC's search portal.

## [0.64.0] - 2026-10-06

### Changed

- A plain-words `search` asks DataCite and Zenodo for the records holding the query in
  their title as a request of its own, beside one for the rest; the two split the
  source's total. The ranking puts a title match first only among the hits it fetched,
  and Zenodo's own order had two snow leopard deposits titled with the name beyond its
  first 200 hits: they now come on page 3 (probed 2026-10-06; DataCite reached them one
  page sooner, page 6 of what was 7). A query with an operator, quote, field or
  wildcard is sent as written, and a multi-query variant is not split. A failed half
  is named in `errors` as `datacite/title` or `zenodo/title`. A cursor minted earlier
  continues as before.

## [0.63.0] - 2026-10-06

### Added

- `errors.next_page` on a `search` page counts the hits already fetched, not yet sent,
  that name every word of the query and pass the filters; `next_cursor` returns them.
  In the snow leopard benchmark task the agents almost never asked for a second page, though six
  pages of "snow leopard" hold 27 of its 35 studies and the first holds 16, and nothing on
  a page said the next was still on topic (`total` counts every loose upstream match).
  The first page of "snow leopard" now says 160 more name it.

### Changed

- A `search` hit leaves out the fields it has no value for (null, `[]`, `{}`); a field
  left out reads as its default. A hit with no data took 500 characters of empty
  fields, so a 50-hit page held 30 to 36 hits; the same snow leopard searches now return
  44 to 50. A script reading hits as plain JSON should treat a missing key as empty.

## [0.62.0] - 2026-10-06

### Changed

- `search` ranks a hit that names the query as written above one that names its words
  apart, and a hit that names it in the title above one that names it elsewhere. For
  "snow leopard", Antarctic station logs ("snow petrels", "leopard seals") tied the snow
  leopard studies and took a share of every page; on 0.61.0, 5 of the 32 hits on the
  first dataset page did not name snow leopard, and now every hit does.
- A deposit held by several sources ranks by the best-described of its copies. The copy
  kept is the most fetchable one, which can have no description (DataONE's), so it sank
  below hits naming nothing, and the DataCite copy behind it stopped DataCite from paging.
- DataCite, Zenodo, BioStudies, OmicsDI and UniProt are sent each plain word of a query
  with its plural ("snow leopard" goes as `(snow OR snows) (leopard OR leopards)`). They
  match words exactly, so "snow leopard" missed studies titled "snow leopards": a 6-page
  dataset search for it now reaches 7 of the 10 snow leopard studies in the head-to-head
  benchmark, up from 4. Quoted phrases, field queries, wildcards, operators and words
  ending in "s" are sent as written, and a cursor from an earlier version keeps its query.

### Fixed

- DataONE copies of Dryad and PANGAEA deposits now carry the deposit's DOI, read from
  DataONE's series ID, so they merge with the same deposit from DataCite or Dryad instead
  of appearing twice without a DOI.

### Security

- The lock file now pins multidict 6.9.1 (CVE-2026-104874) and fsspec 2026.9.0
  (CVE-2026-104851), both indirect dependencies. An install from PyPI resolves its own
  versions within the declared ranges.

## [0.61.0] - 2026-10-05

### Fixed

- A large `search` page no longer gets lost in Claude Code. A 50-hit page could run to
  57,000-427,000 characters, past the ~50,000 Claude Code passes to the model, so the
  page was saved to a file the agent often could not read. A page now holds what fits
  in about 38,000 characters: when fewer than `size` hits fit, `errors.page_size` says
  so and `next_cursor` continues with the rest. Each hit keeps the first few items of
  its lists (creators, links, subjects and others), and `truncated` names each cut list;
  `resolve` still returns the whole record.
- OmicsDI search now pages, and finds every mass-spec dataset it matches. It used to
  fetch one page and then drop the hits from other repositories, so it served page 1
  only and could come back nearly empty: "Chlamydomonas nitrogen" returned 3 of the 36
  datasets. The restriction is now part of the query sent to OmicsDI, and jPOST, iProX
  and Panorama Public are included.
- When NCBI omics (GEO, SRA, BioProject) or OmicsDI matches nothing for a query of two or
  more words, `errors.all_words` now says so. Both return only records holding every
  word, so a longer query can match nothing even when each word matches hundreds.

## [0.60.0] - 2026-10-05

### Changed

- The README is now a short front page: install (with one-click VS Code and Cursor
  links), three example questions with real output, a pipeline diagram, a comparison
  with other servers, and the sources table. The per-tool reference, the HTTP transport
  and the environment variables moved to `docs/reference.md`. The demo is recorded from
  live calls (`examples/_demo_search.py`) instead of a network-free tools listing.
- The default `rank="relevance"` order puts hits that name more of the search first:
  each facet you set (any of its expanded names), then each query word, found in the
  title, description, subjects or organism. It used to take each source's hits in turn,
  so every source's first hit reached the top, named the organism or not: for
  "transcriptome" in *Orobanche aegyptiaca*, a crustacean and a cobra outranked the
  *Phelipanche* transcriptomes. Hits that name as much keep the old order, and nothing
  is dropped.

### Fixed

- A literature search with a long ontology expansion no longer loses OpenAIRE. Its
  search rejects more than four AND/OR/NOT words with HTTP 400, so `tissue="skin"`
  (six UBERON names) dropped every OpenAIRE hit. OpenAIRE is now sent a shorter
  expansion that keeps every facet, the name you typed first, and as many other names
  as fit; `errors["operator_limit"]` lists the names it left out. Other sources still
  get the full expansion, and a query that already fits is sent unchanged.
- A failed `fetch` no longer deletes a file already on disk. Downloads stream into a
  temp file beside the target and replace it only once verified; before, a re-fetch of
  a file with no checksum or size (every GEO supplementary file) truncated the good copy
  and then deleted it if the download failed, for example on `max_bytes`.

## [0.59.0] - 2026-10-04

### Added

- `tissue_expansion.alternatives` and `chemical_expansion.alternatives` list the other
  UBERON/ChEBI terms the name is the label or a synonym of (id + label). For example,
  `skin` is also a synonym of "skin of body", and `glucose` also of "D-glucopyranose".
  The chosen term is unchanged: the one the name is the label of, else the one OLS ranks
  first (`skin` → "zone of skin", whose exact synonym it is in UBERON).

## [0.58.0] - 2026-10-04

### Fixed

- An `organism` name that matches several NCBI taxa no longer resolves to whichever one
  NCBI lists first. NCBI lists them by descending taxid, so `Drosophila` resolved to a
  fungus genus, `fruit fly` to *Drosophila gunungcola* and `bacteria` to a stick-insect
  genus. The name now resolves to the taxon it is the scientific name of, if exactly one;
  otherwise to the taxon with the most nucleotide records of its own. So `Drosophila` →
  the fly genus, `fruit fly` → *D. melanogaster*, `bacteria` → Bacteria. The other
  candidates are listed in the new `taxon_expansion.alternatives` (taxid + name).

## [0.57.0] - 2026-10-04

### Changed

- `list_sources`' `filters_supported` is now derived from each adapter rather than
  written by hand, and it says what each value means. The hand-written lists had drifted
  from the code, so several sources now report different values:
  - **huggingface:** no longer lists `cursor`, `published_after`, `published_before` or
    `kind`. It serves page 1 only and filters nothing upstream.
  - **omics, literature:** no longer list the year and kind filters, which they apply
    only after fetch.
  - **dandi, pdb, gwas, biostudies, cellxgene:** now list `cursor`, because they page.
  - **DataCite:** now lists `size`.
  - **biostudies:** drops `offset`, which `search` does not take.
  - **Ontology facets:** every source that is sent the synonym-expanded query now lists
    `organism`, `disease`, `tissue`, `chemical` and `assay`, not only omics and literature.

  The meaning of each value:
  - `cursor`: the source pages past page 1.
  - the year and kind filters: the source applies them upstream, so its `total` is
    filtered; any other source applies them after fetch.
  - the ontology facets: the source gets the synonym-expanded query.

### Fixed

- Resolving the DOI of a withdrawn Figshare article or OSF registration returns the
  record again. DataCite still lists these DOIs, but the host no longer serves their
  files (Figshare answers 404, OSF 401), and that answer used to fail the whole
  `resolve`; for OSF it was reported as an outage. The record now comes back with its
  DataCite metadata, `files=[]`, and the reason in `truncated["files"]`. An outage at the
  host still fails the resolve.
- `search(collapse_mirrors=true)` no longer folds two records from one repository into
  each other. An older Zenodo version, or a deposit's concept DOI listed through
  DataCite, used to come back as a "mirror" of its own successor and drop off the page.
  It folded on a shared file checksum, or once a copy elsewhere linked the two records.
  Versions now stay separate hits. When a copy in another repository matches several
  records of one deposit, it folds with the latest of them. Which records fold no longer
  depends on the order the sources answered.

## [0.56.0] - 2026-10-04

### Changed

- The provenance crates now conform to RO-Crate 1.1: the dossier
  (`resolve(format="provenance")`), the search run crate (`search(provenance=true)`) and the
  plain `format="ro-crate"` export all pass rocrate-validator's RO-Crate 1.1 profile. Before,
  a JSON-LD reader dropped every assessment field (FAIR scores, `is_latest`, normalized SPDX,
  retraction status, the run's errors and sources) because the crate's context did not
  define them, and the validator failed run crates, and dossiers of records without a
  description or licence. What changes in the output:
  - `@context` is a list: RO-Crate's context, then our own terms, which are defined in
    each crate and documented in [docs/vocab.md](docs/vocab.md).
  - The root always has a `description`, a `license` and a `datePublished`. A record that
    lacks one gets a stand-in that says so (for example "No licence stated"); it never
    gets a licence the source did not give. `license` is now a reference to a licence
    entity (its SPDX page when the licence is recognised), not a string.
  - Nested objects are entities of their own: a run's `errors` become schema.org `error`
    entries, ontology expansions `DefinedTerm`s, and a record's cross-identifiers and
    links `PropertyValue`s.
  - The dossier's `endTime` is when the assessment ran, not the record's last change; that
    change is now the root's `dateModified`.

## [0.55.2] - 2026-10-03

### Fixed

- Resolving an embargoed or confidential Figshare record, directly or through its
  DataCite DOI, failed with a bare `KeyError: 'files'`. Figshare now leaves the file list
  out of such an article instead of sending it as null; the record resolves again, with
  no files, as before.

## [0.55.1] - 2026-10-03

### Fixed

- Query understanding and semantic re-rank work with a local model server again. The
  private-address guard refused an `LLM_API_BASE` or `EMBEDDING_API_BASE` on this machine
  or your network (Ollama on `127.0.0.1:11434`), so both were skipped unless
  `DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS=1` turned the guard off for record URLs too.
  The endpoints you configure are now exempt; a record URL pointing at the same server,
  and a redirect away from it, are still refused.

## [0.55.0] - 2026-10-03

### Security

- `operate` checks every redirect against the private-address guard, as `fetch` already
  did. It checked only a file's first URL, so a redirect could make the server read an
  internal address
  ([GHSA-9q96-qp9g-h7cv](https://github.com/musharna/data-aggregator-mcp/security/advisories/GHSA-9q96-qp9g-h7cv)).

### Fixed

- `resolve(format="ro-crate")` produces a valid RO-Crate 1.1 crate for records with
  authors. Each author was written inside the dataset entry without an id, which
  RO-Crate 1.1 does not allow (the RO-Crate validator rejected every such crate), and
  the author's ORCID was left out. Each author is now its own `Person` entry,
  identified by its ORCID when the record has one; `author` lists them by id.
- The provenance crate of `search(provenance=true)` lists only data sources under
  `sources_queried`. It also listed every note in the search's `errors` (`semantic`,
  `filters`, `query_syntax`, a failed ontology lookup such as `taxonomy`) as a source,
  and named a failed sub-database or multi-query stream by its internal key
  (`omics/sra`, `zenodo#v1`) instead of as its records do (`sra`, `zenodo`). The notes
  are still disclosed verbatim under `errors`.
- An OpenAIRE search answer missing its result list or hit count is reported as a
  malformed answer instead of zero hits, a record answer that is not a record (an
  empty object or an error message) is no longer returned as an untitled record, and
  a wrong-typed field no longer escapes as a bare error.
- Resolving an `openaire:` id of a dataset, software or other research product
  reports that kind. Every OpenAIRE record was called a publication, although only
  search is limited to publications.
- Asking for a prompt without its required argument (`find_data` with no `topic`) is
  refused with an "invalid params" error that names the argument. The prompt was
  rendered with a hole in it ("find datasets about: ."). An unknown prompt name and an
  argument the prompt does not take (a misspelt `organsim`) are refused the same way;
  the misspelt argument used to be dropped without a word.
- The `search` tool's description names all 17 sources a search queries. It named 12,
  leaving out GBIF, data.gov, NASA CMR, UniProt and BioStudies, which were searched
  all along.
- Resolving a Dataverse file DOI works. Dataverse gives each file a DOI as well as
  each dataset (85 of 100 random Harvard DOIs registered as datasets are file DOIs),
  and resolving one failed as "not found"; it now lists that file. A DOI the
  Dataverse installation no longer holds gives the record with no files instead of
  failing, as a withdrawn (deaccessioned) dataset already did.
- Dataverse files under an embargo that has not ended, or past their retention
  period, are no longer listed. Dataverse refuses to download them (HTTP 403), the
  same as restricted files, which were already left out.
- Dataverse files keep their folder in their name (`data/2026-06-24.tsv`), so a
  fetch keeps the dataset's layout and two files with the same name in different
  folders are no longer saved side by side with a hash added to one name.
- Files from Dataverse installations that use SHA-1 checksums (DataverseNL) are
  listed with their checksum, so a fetch can verify them; they were listed with none.
- A Dataverse answer missing its dataset or file, or with a wrong-typed field, is
  reported as a malformed answer instead of no files or a bare error.
- A search `cursor` that no search could have produced is refused as an invalid or
  corrupt cursor before anything is requested. A cursor listing more query variants
  than a multi-query search makes (at most 4) sent one request per variant to every
  source; one listing none answered with an empty page; an unknown `kind` or an
  unexpected filter emptied every page; and a filter or `sources` value of the wrong
  type failed with a bare Python error. Cursors returned by `search` are unaffected.
- `fetch` with `extract=true` unpacks a `.tar` whose last member is a `.zip` as the
  tar. It was unpacked as that inner zip: the tar's own files were missing and the
  zip's files were written in their place. The archive's name now decides its format.
- An archive that cannot be read (corrupt, truncated, encrypted, or compressed with a
  method Python lacks, such as Windows' Deflate64) is reported as an
  `UpstreamUnavailableError` naming the archive, instead of a bare `BadZipFile`,
  `EOFError`, `NotImplementedError` or `RuntimeError`. A zip member named `.` or `a/..`
  is refused instead of failing with `IsADirectoryError`.
- An extraction that fails part-way (over `max_bytes`, a link or escaping member, an
  unreadable member) removes the files it had already unpacked; until now they were
  left in the fetch directory, up to `max_bytes` of them. A file repeated in an archive
  is listed once in `paths`.
- A client that declares elicitation support as a bare `elicitation: {}` is now asked
  to correct an organism, disease, tissue, chemical or assay term that matches
  nothing. The MCP specification treats that declaration as form support, and it was
  the only way to declare elicitation before protocol version 2025-11-25, but the
  server read it as no support, so the search ran without the filter and never asked.
- `search(collapse_mirrors=true)` compares the first author's whole name. It
  compared only the last word, which for a "Family, Given" name (most DataCite and
  Zenodo records) is the given name, so distinct datasets with the same generic title
  and year were folded together when their first authors shared a given name, and a
  copy whose author was written "Given Family" in one repository and "Family, Given"
  in another was not folded.
- `search(collapse_mirrors=true)` keeps the earliest of the best-ranked copies as the
  survivor and lists the folded copies in result order. When copies were linked only
  through a chain of shared file checksums, a later copy could be kept instead.
- `rank=semantic` no longer fails the search, or reorders it on numbers that are not
  similarities, when the embedding endpoint sends a malformed answer. A text or null
  value in a vector raised an error out of the search, and a NaN, an infinity, a
  vector of the wrong length or rows out of order were ranked as if they were real;
  such an answer now leaves the results in relevance order with the usual
  `errors["semantic"]` note, and the failure is logged on the server.
- An `EMBEDDING_API_KEY` that is not a valid bearer token (a non-ASCII character, a
  space or a line break, e.g. from a pasted key) is refused with a log line that
  does not quote it, and semantic re-rank is skipped; a non-ASCII key used to fail
  inside the HTTP library.
- An `EMBEDDING_API_BASE` that is not an http(s) URL with a host (no scheme, another
  scheme, an unparseable host such as `http://xn--/v1`) is refused before any request,
  with a log line saying so, and semantic re-rank is skipped. It used to be retried as
  a network failure or skipped without any message.
- `resolve(format="croissant")` manifests load in MLCommons' Croissant validator
  (`mlcroissant`). The manifest's `@context` declared only namespace prefixes, so the
  validator failed on every manifest before checking it, and `conformsTo`, `citeAs`
  and `md5` were schema.org names rather than the Croissant terms. The context is now
  the one MLCommons' Croissant 1.1 examples use. A file whose name has a space (2 of 24 live
  records sampled) no longer fails the whole manifest; its `@id` is the
  percent-encoded name, and `name` is unchanged. `contentSize` is text in bytes
  (`"10 B"`), not a number. A file whose source gives no MIME type or checksum is
  still listed without them, and the validator reports it as incomplete.
- `list_sources` gives Zenodo an example id that can be fetched (`zenodo:1254563`).
  The old example, `zenodo:7654321`, led to a restricted record with no files, so
  fetching it failed.
- A record whose file URL has a host that is not a hostname (an empty label, as in
  `http://a..b/`, or a label over 63 characters) is refused by `fetch` and `operate`
  as a malformed URL, naming the file and the URL. It was a bare "'idna' codec can't
  encode character ..." naming neither.
- `relate` no longer calls records "the same work" because they share a taxon or a
  gene. Any two human UniProt entries were joined on taxon 9606, and a protein was
  joined to PubMed article 9606. A shared identifier is now a DOI, PubMed id or PMC id
  matched within its own kind, and the hint says which kind.
- `relate` gives the same answer every time for the same records. When two records
  shared more than one identifier, the order of those hints, and which spelling of a
  DOI was shown, could change after a server restart.
- `relate` finds a link given as an `ncbi.nlm.nih.gov/pubmed/<id>` address, an OpenML
  `search?type=data&id=<id>` page, or an RCSB page for an entry cited before release
  (`structure/unreleased/<id>`). Records that cite another record this way (an ICPSR
  study cited by a PubMed article, a Zenodo deposit referencing an OpenML dataset) got
  no link hint.
- `understand=true` searches the years a query names even when the model gives them
  high to low. Asked for "arabidopsis root datasets between 2018 and 2016", llama3.1
  answered `year_min` 2018 and `year_max` 2016, and the search returned no results and
  no error; the years are now applied as 2016 to 2018.
- Resolving a renamed HuggingFace dataset by its old name (`hf:imdb`, now
  stanfordnlp/imdb) lists its converted Parquet files. The datasets-server lookup used
  the old name, which it answers with "not found", so the dataset looked unconvertible
  and could not be queried with `operate`.
- A HuggingFace datasets-server answer missing its file list is reported as a
  malformed answer instead of "no converted files", a converted file with a missing
  or wrong-typed field fails the answer instead of being dropped in silence, and a
  wrong-typed field no longer escapes as a bare error.
- A HuggingFace split that the dataset viewer converted only in part (its first 5 GB)
  is listed as `<config>/partial-<split>/…` instead of `<config>/<split>/…`, so a
  query on it no longer passes for one over the whole split (825 of allenai/c4's
  1,006 converted files). The files of a split converted in several parts
  (`<split>-part0`, `-part1`, …) now have distinct names; they shared one, and only
  the first could be picked with `file=`.
- Resolving a gated HuggingFace dataset, a dataset built by a loading script, or one
  whose converted file list is too large to list no longer reports a failed
  datasets-server lookup. The dataset viewer answers these with HTTP 401 or 501, which
  was reported as an outage, so the record carried an error and was never cached; it
  now resolves with its repository files, like any dataset without a converted view.
- `resolve(trust=true)` reports a withdrawn, removed or partly retracted paper as
  retracted, with the notice as `retraction_doi`. Crossref records these as their own
  update types (3,397 withdrawals, 702 removals and 2 partial retractions on
  2026-10-02), and only a notice of type `retraction` was read, so such a paper, or a
  record built on it, was reported as having no retraction on record. Update types
  spelled `Retraction` or `expression-of-concern` are read as well.
- A Crossref answer that holds no work, or whose list of updates cannot be read, now
  leaves the retraction status unknown. It was reported as "not retracted".
- Reading a resource whose URI carries a query, a fragment, a user name or a port
  (`dataresource://record/pdb%3A1bg2?v=2`, `…#x`, `dataresource://u@record/…`) is
  refused as not a readable resource. Those parts were ignored, so the URI read a
  record it did not name, and an id sent without URL-encoding was cut at its first
  `#` or `?` (some Wiley DOIs end in `#`). URL-encode the id, as the record template
  says.
- `chemical=` and `tissue=` find the ChEBI or UBERON term whose name or synonym is
  the one given even when EBI OLS ranks it low. The lookup took OLS's ten most
  relevant terms, and for common names the match was not among them, so
  `chemical="aspirin"`, `tissue="skin"` and `tissue="bone"` were reported as
  unresolved and the search was not expanded. A name with spaces around it is now
  matched too.
- An EBI OLS answer for `chemical=` or `tissue=` that is missing its result list or
  has a wrong-typed field is reported in `errors` as a lookup failure instead of
  "no match", and is no longer remembered as "no match" for an hour.
- `assay=` finds the EDAM topic whose name or synonym is the one given even when
  EBI OLS ranks it low, the same fix as for `chemical=` and `tissue=`:
  `assay="Genes"` (a synonym of Genetics) was reported as unresolved. An EBI OLS
  answer for `assay=` that is missing its result list or has a wrong-typed field is
  reported in `errors` as a lookup failure instead of "no match", and is no longer
  remembered as "no match" for an hour.
- The `resolve(fair=true)` score weights each indicator by the priority the RDA FAIR
  Data Maturity Model gives it. Eight of the fourteen indicators had a different
  weight (for example F3 and F4, which the specification calls Essential, counted as
  Important), so every score was off. The gaps about the data's download protocol,
  file formats and community standard now carry the specification's data ids
  (`RDA-A1.1-01D`, `RDA-I1-01D`, `RDA-R1.3-01D`) instead of the metadata ones.
- The FAIR score no longer marks a download over `http://` or `ftp://` as lacking a free
  access protocol (the specification names both), counts a compressed file such as
  `reads.fastq.gz` as a recognised format, and no longer credits provenance to every
  record that has creators: it needs funding, a modified date or related records, as
  its gap says. A record with no licence is no longer told its licence is "free text".
- Resolving a PubMed id that is not a number says the record is not found, before
  any request. NCBI reads its id parameter as a list, so `pubmed:34320281,1` came back
  as PMID 34320281 carrying PMID 1's data links and both papers' abstracts, and
  `pubmed:abc` was reported as an NCBI outage.
- A paper that links to more than 100 SRA, GEO or BioProject records resolves, with
  the first 100 of each and a note in `truncated["links"]`. PMID 42544407 links to
  7,498 SRA records, and resolving it failed with a bare `InvalidURL` error, because
  every linked record was asked for in one request (NCBI accepts at most 500).
- Paging a PubMed search past its 9,999th record says that PubMed stops there,
  instead of reporting an NCBI outage on every later page. PubMed's search API serves
  only the first 9,999 records of a query.
- A PubMed summary with a wrong-typed field, or without a PMID or title, is reported
  as a malformed answer instead of escaping as a bare error or coming back as
  `pubmed:`.
- `list_sources(check_health=true)` reports a source as up only when its upstream
  gives an answer the source can use. It counted any status below 400 as up, so a
  maintenance page served with HTTP 200, an NCBI error inside a 200, or a redirect
  showed the source as up. The literature probe asked Europe PMC, which literature
  search does not use; it now asks PubMed. DataCite and omics are now probed at the
  search endpoints they use, instead of DataCite's heartbeat and NCBI's database list.
- An SRA record's FASTQ list from ENA is reported as a malformed answer when its
  size or checksum list does not line up with its file list. Each file used to take
  the size and checksum at its own position, so a list missing an entry gave the
  files after it the wrong size or checksum. A listed file path that is not on
  ftp.sra.ebi.ac.uk is refused instead of becoming a download URL, and a malformed
  record no longer escapes as a bare error.
- An OSF file listing that comes back malformed is reported as a malformed answer
  instead of "no files" or a manifest cut short, a file without a name or download
  link is no longer listed, a wrong-typed field no longer escapes as a bare error,
  and a paging or folder link that leaves `api.osf.io` is not followed. A DataCite
  DOI whose last part is not an OSF id sends no OSF request.
- When `understand=true` or `multi_query=true` cannot use the LLM endpoint, the server
  log says why. Every failure (an error status, a malformed answer, an unreachable
  endpoint, the egress guard refusing a local server) used to show only "query
  understanding unavailable", with nothing logged. Each now logs one warning naming the
  cause, with the API key removed from it. An `LLM_API_KEY` with a line break (as a
  `.env` file with Windows line endings leaves it) or a non-ASCII character, and an
  `LLM_API_BASE` that is not an http(s) URL, are refused before any request. A
  line-broken key or a base without `http://` used to be retried as a network failure,
  adding about 3 seconds per LLM call. An answer without a message is retried like any
  other malformed answer.
- An `organism=` or `disease=` search whose NCBI Taxonomy or MeSH lookup comes back
  malformed (an error envelope, or a record without its id, name or entry terms) reports
  the failure in `errors` and runs the query un-expanded. Before, the lookup was read as
  "no match" and remembered for an hour, so the same name kept running un-expanded
  after NCBI recovered. A MeSH record whose entry terms came back as one string no
  longer turns the query into one-letter terms.
- Resolving a NASA CMR id that is not a collection concept id (`nasacmr:foo`, a
  lower-case `c…` id, a trailing space) says the collection is not found. CMR refuses
  such an id with HTTP 400, and the adapter reported it as a NASA CMR outage.
- A NASA CMR answer missing its hit count or collection list is reported as a
  malformed answer instead of zero hits, a collection without a concept id is no
  longer returned as `nasacmr:`, and a wrong-typed field no longer escapes as a bare
  error.
- data.gov datasets keep every theme as a subject. Some publishers give a theme as a
  labelled concept instead of plain text (70 of 3,472 datasets sampled), and those
  themes were dropped.
- A data.gov answer missing its result list is reported as a malformed answer instead
  of zero hits or "no such dataset", and a wrong-typed field no longer escapes as a
  bare error.
- An OmicsDI record's `doi` is the dataset's own DOI, not the DOI of the paper that
  describes it. Every OmicsDI record with a publication reported the paper's DOI as
  its own, so a dataset and its paper could be taken for the same record; the paper's
  DOI is now a `described_in` link, and a PRIDE dataset's own DOI is filled in.
- OmicsDI MetaboLights records keep their paper's DOI and PubMed id when the
  publication is written "title. DOI. PMID:n"; the DOI was dropped (3 of 4 records
  sampled) and the PubMed id never read.
- An OmicsDI id whose repository or accession contains `/`, `..`, `?`, `#` or `%` is
  refused before any request; `omicsdi:pride:../../evil?injected=1` reached another
  OmicsDI endpoint.
- An OmicsDI answer missing its result list is reported as a malformed answer instead
  of zero hits, and a wrong-typed field, or a record for a different accession, no
  longer escapes as a bare error or comes back as the wrong record.
- A malformed PRIDE file listing (for an `omicsdi:pride:` dataset) is reported as a
  malformed answer. A file entry of the wrong shape escaped as a bare error, an entry
  without a name was listed as a file named "", and a file count of `true` was read
  as one file.
- A Hugging Face search or resolve no longer fails on a dataset whose card lists its
  licences instead of naming one. A card with `license: []` (for example
  `priyank-m/SROIE_2019_text_recognition`) made the whole search return no Hugging Face
  results, and the dataset could not be resolved. The first listed licence is used,
  or none.
- A malformed Hugging Face answer (a dataset without an id, or a field of the wrong
  type) is reported as a malformed answer instead of escaping as a bare error or
  becoming a record with the id `hf:`.
- `operate` `schema` and `preview` name the first column of a CSV that starts with a
  UTF-8 byte-order mark (as Excel's "CSV UTF-8" writes it) without the mark, as `head`
  and `sql` already did. A column name copied from `preview` into a query now matches.
- `operate` `preview` of a CSV no longer returns half a row when its 64 KB read ends
  inside a quoted field that holds line breaks (a poem, an address, a long comment).
  The half row came back as data, its later columns empty; it is now left out and the
  page is marked `truncated`.
- An NCBI failure is reported as an error instead of an empty answer. When NCBI's
  summary service failed, PubMed reported a real PMID as "no record"; when its link
  service failed, a PubMed record or BioProject came back with no data links; and a
  search answer without its result read as zero hits. A wrong-typed field in an NCBI
  answer no longer escapes as a bare error.
- A GWAS Catalog answer missing its study list or total is reported as a malformed
  answer instead of zero hits, a study answer without an accession is no longer
  reported as "no such study", and a wrong-typed field no longer escapes as a bare
  error. A trait with no studies, and a page past the last one, still come back empty.
- Resolving a GBIF dataset that GBIF has deleted reports it as not found, with the
  date it was deleted. GBIF still answers with the deleted dataset's record, so it came
  back as a live dataset whose archive link no longer works (GBIF lists 25,205 deleted
  datasets).
- A GBIF id that is not a dataset key (GBIF keys are UUIDs) is reported as not found
  instead of as a GBIF outage.
- A GBIF answer missing its result list or count is reported as a malformed answer
  instead of zero hits, and a wrong-typed field no longer escapes as a bare error or
  turns a keyword string into one-letter subjects.
- `operate` SQL accepts a `;` inside a string (`WHERE go = 'GO:1;GO:2'`), a query that
  ends in a `--` comment, and DuckDB's other query forms (`FROM data WHERE ...`, a
  leading comment). It still refuses anything that is not a single query.
- `operate` SQL always returns at most its row cap. A query ending in `) --` used to
  switch the cap off, so the server read every row of the result into memory.
- `operate` `peek` profiles a file that has a header and no rows. It used to fail with
  a Python error; the null percentage is now empty for such a file.
- An `operate` `sql`, `head` or `peek` that runs past the wall-clock limit is stopped.
  The limit used to report the timeout while the query kept running in the server to
  its end, using its CPU and memory. Limitation: a source file still downloading when
  the limit is reached finishes downloading before the work stops; stopping a download
  part-way would need a separate process.
- A GEO record of several organisms lists each one. GEO names them in one field
  ("Homo sapiens; Mus musculus"), which the adapter kept as a single organism, so the
  record's taxa held only one of them.
- An NCBI summary the omics adapter cannot read (a field of the wrong type, no
  accession, SRA experiment XML that does not parse) is reported as upstream trouble
  naming the database and uid, instead of escaping as a bare error or becoming a record
  with an empty id.
- `resolve` with `cite=` no longer returns a web page or a different format as the
  citation. For DOIs whose registration agency cannot produce the format asked for,
  doi.org answers with something else: Chinese ISTIC DOIs with the publisher's HTML
  page, Taiwanese Airiti DOIs with CSL-JSON for every format. The citation is now
  null in that case, as for any other citation that cannot be rendered.
- An OmicsDI MetaboLights study's file list is read only from that study's directory
  index on the EBI mirror. Any other page answered there (an error or maintenance
  page, another study's index) was read as the study's files, listing the page's links
  or no files at all; now it is reported as an upstream error. A listed link that is
  not a file of that directory is refused instead of being fetched from elsewhere on
  the mirror, and only `MTBLS<number>` accessions are looked up.
- MetaboLights file names that the mirror's listing escapes (a space, `#`, `%`, `:`
  or a non-ASCII letter) are reported as the file's real name and keep their published
  sha256; they came back escaped (`a%20b.txt`) and unverified.
- Resolving a Figshare collection DOI (`10.6084/m9.figshare.c.8708104.v1`, and the
  same `.c.` form on institutional portals) returns the collection's record. The
  collection number was taken for an article number, Figshare answered "not found",
  and the whole resolve failed although DataCite holds the DOI.
- Records from Griffith University's Figshare portal (`10.57831/<number>` DOIs) list
  their files. The article number comes straight after the slash in those DOIs and
  was not recognised, so they came back with no files.
- A Figshare answer missing its file list, or with a wrong-typed field, is reported
  as a malformed answer instead of "no files" or a bare error. A Figshare article that
  carries no DOI of its own no longer has its files attached to the DOI that was
  asked for, since nothing shows it is the same article. An embargoed article still
  comes back with no files.
- Resolving a Dryad DOI that Dryad's API does not hold returns the DataCite record
  with no file list instead of saying the DOI does not exist. More than half of the
  Dryad DOIs DataCite lists (95,914 of 170,511) are single files from Dryad's old
  repository, such as `10.5061/dryad.50kt0/1`, and none of them could be resolved; the
  record links to the dataset it is part of, which lists its files.
- A Dryad answer missing its version link, file list or file count is reported as a
  malformed answer instead of an empty or shortened file list, a wrong-typed file field
  no longer escapes as a bare error or turns into a wrong size or checksum, and a
  pagination link that is not a path on datadryad.org is refused instead of being
  requested from another host.

## [0.54.14] - 2026-10-02

### Fixed

- A search asking for more than 25 results returns Zenodo's results again. Zenodo
  refuses an anonymous page over 25 records, and the adapter asked for up to 50, so
  every search for 26-50 results came back without Zenodo. The `search` tool's limit
  stays 50.
- A Zenodo answer missing its record list or total is reported as a malformed answer
  instead of zero hits, and a wrong-typed field no longer escapes as a bare error.

## [0.54.13] - 2026-10-02

### Fixed

- DataONE `search` returns only the latest version of each dataset, as DataONE's own
  search does. Every update leaves the old version in the index, and 108,568 of
  110,671 hits for "salmon" were superseded copies; the total counted them too.
- A DataONE answer missing its result list or count is reported as a malformed answer
  instead of zero hits or "no such object", and a wrong-typed field no longer escapes
  as a bare error.

## [0.54.12] - 2026-10-01

### Fixed

- DataCite creators named only by their given and family names keep their names
  ("Kluth, Yannick"). Over 90,000 DataCite records list creators that way, and they
  came back as nameless authors.
- A DataCite record's description is its abstract and its title is its main title
  when the record lists other kinds first (a contact block, a journal series line, an
  alternative title).
- A DataCite answer missing its record, record list or total is reported as a
  malformed answer instead of "no such DOI" or zero hits, and a wrong-typed field no
  longer escapes as a bare error.

## [0.54.11] - 2026-10-01

### Fixed

- OpenML `resolve` reports a dataset OpenML does not have as not found instead of as
  an outage. OpenML answers an unknown id with HTTP 412 (error code 111), which was
  read as the service failing. `openml:0` is refused as malformed without a request.
- A zero-padded OpenML id (`openml:0061`) resolves as its dataset (`openml:61`). It
  was sent as typed, came back as a second id for the same dataset, and carried a
  Parquet URL that does not exist.
- An OpenML record or search answer missing the object the adapter reads is reported
  as a malformed answer instead of "no dataset" or zero hits, and a wrong-typed field
  or an unexpected 412 body no longer escapes as a bare `AttributeError` or
  `TypeError`.

## [0.54.10] - 2026-10-01

### Fixed

- BioStudies file download URLs escape the file's path. A name with a space gave an
  invalid URL (BioImage Archive's S-BIAD2787 has 92 such files), and a `#`, `?` or `%`
  in a name requested a different file: `well #3.tif` asked for `well `.
- Hugging Face file download URLs escape the file name the same way (`omar87/pdf-laws`
  has 20 files with spaces in their names).

## [0.54.9] - 2026-10-01

### Fixed

- OpenNeuro file manifests and PDB records report a broken GraphQL answer as a failure
  instead of an empty one. A 200 without a `data` object read as an empty OpenNeuro
  manifest or "no PDB entry"; a string where a list belongs raised a bare error out of
  `resolve`; an OpenNeuro `urls` string became the download URL `"h"`. An OpenNeuro
  file listed without a download URL now stays in the manifest (fetch reports it
  skipped) instead of being dropped from it.

## [0.54.8] - 2026-10-01

### Fixed

- Full-text, PMC-id and Scholix data-link lookups report a broken answer as a failure
  instead of an empty one. EuropePMC answers a bad request with a 200 carrying
  `errCode`, which read as "no open-access copy"; an NCBI idconv answer without
  `records` read as "not in PMC"; a ScholeXplorer answer without `result` read as "no
  data links". A Scholix outage, or a 200 carrying a JSON list, raised out of `resolve`
  for an OpenAIRE record; the links are enrichment, so the record now resolves with
  the failure in `errors["links"]`, as the other lookups already did. A Scholix 404 is
  a named failure too: ScholeXplorer answers a DOI with no links with a 200 and an
  empty list.

## [0.54.7] - 2026-10-01

### Fixed

- `uniprot` search reports a broken answer as an error instead of zero hits. Its body
  skipped the type check every other source has had since 0.54.2, so a 200 carrying
  `null` read as no results and a list or a string raised `AttributeError`; and a 404
  read as no results, while UniProt answers a search with no hits with a 200 and an
  empty list. A malformed body is now retried, then an `UpstreamUnavailableError`; a
  404 is a `NotFoundError`.

## [0.54.6] - 2026-10-01

### Fixed

- A `Retry-After` header is read as RFC 9110 defines it: a whole number of seconds or
  an HTTP-date. A date was ignored, so a server asking for a later retry got the 1 s
  backoff, and `Retry-After: nan` made the retry raise an untyped `ValueError`. Any
  other value now falls back to the backoff; the 60 s cap is unchanged.

## [0.54.5] - 2026-09-30

### Fixed

- `zenodo`, `dandi`, `gwas`, `openml` and `huggingface` resolve reject a malformed id
  before sending a request. Each put the id into the request path unchecked, so an id
  such as `zenodo:../../x?y=1` reached another endpoint or added query parameters, as
  `cellxgene` did before 0.54.4. A Zenodo, OpenML or DANDI id must be digits, a GWAS id
  `GCST` and digits (any case), and a HuggingFace id `name` or `owner/name`.

## [0.54.4] - 2026-09-30

### Fixed

- `cellxgene` resolve accepts only a collection UUID. The id went into the request path
  unchecked, so `cellxgene:../collections` downloaded the ~3 MB collection list twice and
  was reported as a CELLxGENE outage, and an id with `?` or `/` reached other endpoints
  and query parameters. Any other id is now "malformed", before a request is sent; an
  upper-case or padded UUID is canonicalised.
- `cellxgene` resolve no longer reports a CELLxGENE error as "no such collection". A
  missing collection is a 404; a 200 that names no collection (an error envelope) is now
  retried and then reported as an upstream failure.
- `cellxgene` search no longer lists a collection that has no id as an unresolvable
  `cellxgene:` record, and a file with no title or type is named from its URL's path
  without the query string.

## [0.54.3] - 2026-09-30

### Fixed

- `pdb` no longer reports an RCSB failure as "no such entry" or as an empty search.
  RCSB answers a GraphQL query it rejects (for example, after renaming a field the
  adapter asks for) with HTTP 200, an `errors` list and no data. Resolve then raised
  "RCSB PDB has no entry", and search returned the hit count with no records. Both now
  raise an upstream failure that quotes RCSB's error message.
- `pdb` search no longer reads a 200 that lacks the hit count or the hit list as zero
  results. RCSB sends zero hits as 204 No Content, so such a 200 is malformed: it is now
  retried and then reported as an upstream failure.

## [0.54.2] - 2026-09-30

### Fixed

- `dandi` resolve no longer returns a stripped-down record when DANDI's version-info
  endpoint answers 200 with `[]` or `null`. The record came back with no metadata
  title, DOI, licence or authors, and no error. Such a body is now retried and then
  reported as an upstream failure; a 404 still falls back to the dandiset's own fields.
- `dandi` resolve no longer reports a DANDI outage as "no such dandiset". A 200 from the
  dandiset endpoint carrying `null` or `[]` was read as a missing dandiset (and a
  non-empty list crashed with `AttributeError`); it is now retried and then reported as
  an upstream failure. A 404 is still reported as not found.
- Every source now reports a 200 whose JSON is the wrong type (`null`, `[]`, a bare
  string, or an object where a list was promised) as an upstream failure, retried and
  named, instead of an empty result, "not found", or an untyped `AttributeError`. On a
  body like that, 22 search/resolve entry points across biostudies, datacite, datagov,
  dataone, gbif, gwas, huggingface, literature, nasacmr, omicsdi, openml, pdb, uniprot
  and zenodo raised `AttributeError`; `cellxgene` resolve said the collection did not exist;
  the UBERON, EDAM and ChEBI lookups cached it as "no such term". Internally,
  `_http.request_json` now requires `expect=`, so each call site states the body it accepts.

## [0.54.1] - 2026-09-30

### Security

- A licence URL with a password in its userinfo no longer passes as a real licence.
  `http://creativecommons.org:x9@evil.example.com/licenses/by/4.0/` points at
  evil.example.com, but it was identified as `CC-BY-4.0` and `check` answered ALLOW. The
  guard rejected only `host@` and `host:<port>@`; any other password (or an empty one)
  got through. Now everything before an `@` in a URL's authority is treated as userinfo.
- `fetch` no longer hangs the server on a record whose file list puts a path under a
  name that is also a file (`Data`, then `data/x.csv`). Planning the on-disk paths
  renamed only the file, never the directory that clashed, so it looped forever on
  the event loop and every other request stalled with it. File names come from the
  uploader. The clashing directory is now renamed `data~<hash8>/`, and every path
  that planned before plans the same, so resumed fetches still find their files.

## [0.54.0] - 2026-09-29

### Fixed

- `datacite` search results are now ranked by relevance. The adapter sent no sort,
  and DataCite's default order is most recently updated first, so a search returned
  the matching records edited in the last few minutes and page 2 shifted whenever
  one was touched between requests. Records with tied scores can still swap order
  between requests, so a tied record can occasionally appear on two pages.

## [0.53.0] - 2026-09-28

### Fixed

- `gwas` searches and resolves no longer fail as "unreachable" while the GWAS Catalog
  answers. Its v2 API takes 21-33 s per request, and the 30 s timeout abandoned
  replies that arrived at 31-33 s, on every retry alike. The timeout is now 60 s.
  GWAS calls stay slow (a search can take about a minute), and the Catalog's own
  HTTP 500s at ~30 s are still retried and then reported.

## [0.52.0] - 2026-09-28

### Fixed

- A record whose file URL does not parse (an unclosed IPv6 bracket, an out-of-range
  or non-numeric port) is refused by `fetch` and `operate` with an error naming the
  file and the URL. It was a bare "Invalid IPv6 URL" or "Port out of range" naming
  neither (#85). A malformed Dataverse landing URL no longer breaks the file listing.
- A non-numeric TaxId from NCBI is reported as an upstream error naming the value, not
  "invalid literal for int()".

## [0.51.0] - 2026-09-28

### Fixed

- The `gwas` source works again. The GWAS Catalog retired its v1 REST API (kept
  until May 2026), which now refuses every request with HTTP 429, so every GWAS
  search and resolve failed as "rate limited". It now uses the v2 API, with the same
  exact disease-trait matching. A study's paper title and year come from a second
  request on `resolve`; if that fails, the record says so in `errors["publication"]`
  and is not cached. Search results are titled by the trait.

## [0.50.0] - 2026-09-28

### Fixed

- A literature or Hugging Face record whose enrichment lookup failed is reported in
  `errors` and no longer cached: the NCBI ID Converter (`identifiers`), the EuropePMC /
  Unpaywall full-text check (`files`), the PubMed abstract (`description`) and the
  Hugging Face datasets-server parquet list (`files`). Each failure came back as the
  same value as a real "none", so during a EuropePMC outage an open-access paper was
  cached with no full text and read as "no open-access copy" for the cache TTL.

## [0.49.0] - 2026-09-28

### Fixed

- A failed Zenodo latest-version lookup, or a failed ScholeXplorer / doi.org lookup
  behind a publication's data links, is reported in the record's `errors`
  (`superseded_by`, `links`) and the record is no longer cached. The degraded record
  read as "no newer version", "not a DataCite DOI" or "no data links" for the cache TTL.
- DataCite records from Harvard Dataverse, DataverseNO and Mendeley Data carry their
  licence (e.g. `cc-by-4.0`). Those publishers list an access status first, and the
  licence was read from that entry, so it came back empty. The status now sets
  `access`: an embargoed CC-BY record is `embargoed`, not `open`.
- A BioProject's SRA links list each run once. NCBI returns the runs in two
  overlapping link sets, which were concatenated, so every run appeared twice.

### Changed

- Zenodo and DataCite records whose type none of the kinds covers (images, posters,
  audiovisual, untyped) are kind `other`. They were `dataset`, so they passed a
  `kind=dataset` filter: 7,853 of Zenodo's and 2,206 of DataCite's "soil moisture" hits.

### Added

- Records carry `truncated` (`{field: note}`) when a list is deliberately partial. A
  BioProject with more than 100 SRA runs says `first 100 of 891 SRA runs; …` instead of
  only logging it server-side.

## [0.48.0] - 2026-09-28

### Fixed

- `published_after` / `published_before` / `kind` filters reach Zenodo and DataCite, so
  those sources return matching records and a filtered `total`. They were applied only
  to the first fetched window: "soil moisture" 2019–2020 returned 0 results against a
  total of 127,525.
- Fetching a large PDB entry (e.g. 4V6X) works: resolve lists the legacy `.pdb` file only
  when wwPDB makes one. It listed one for every entry, and the missing file's 404 failed
  the whole fetch.
- Dryad, NASA CMR and other records `fetch` refuses no longer advertise `fetch` or the
  operate modes in `access_modes`, and `operate` refuses them with fetch's reason instead
  of failing on the download with only the URL as its error.
- DANDI and MetaboLights downloads are checksum-verified: both publish a sha256 per file
  (DANDI in each asset's metadata, MetaboLights in the study's `HASHES/`), which the
  manifests now carry.
- A resolve whose taxonomy enrichment failed (e.g. an NCBI rate limit) is no longer
  cached for the TTL, so the next resolve retries it.

### Changed

- Every tool refuses an argument it does not declare, with an input-validation error.
  It was silently dropped: `search(limit=3)` returned 10 results (the argument is `size`).
- Records carry `errors` (`{step: message}`), naming an enrichment step that failed on
  the record; empty when none did.
- Sources that cannot take those filters upstream (omics, literature, HuggingFace) are
  still filtered after fetch, and a page the filters thinned now says so in
  `errors["filters"]`: which sources, how many records were removed, that their `total`
  is unfiltered, and that `next_cursor` continues.

## [0.47.0] - 2026-09-27

### Fixed

- data.gov search, resolve and fetch work again. data.gov retired its CKAN API (every
  call 404ed), so the source now uses the new DCAT-US catalog API, which needs no key;
  `DATA_GOV_API_KEY`, when set, routes through the api.gsa.gov v4 gateway.
- Resolving a GEO sample, platform or DataSet id returns that record instead of a related
  series (`geo:GSM613466` resolved to `GSE24974`, `geo:GPL570` to an unrelated series).
- A deleted or merged UniProt entry raises NotFoundError with UniProt's reason instead of
  resolving normally and "fetching" an empty FASTA.
- A Figshare DOI ending in `.vN` fetches that version's files, not the latest version's.
- File manifests are complete: OSF lists files inside folders, Dryad and PRIDE read every
  page (not the first 20 / 100), OpenNeuro lists the whole snapshot tree including
  subject folders, and BioStudies reads studies' external file lists. Each walk raises
  rather than returning a partial list.
- Dataverse tabular files that were converted on upload are listed as the original
  upload their md5 describes, so fetch's checksum check passes.
- Superseded Zenodo and Mendeley versions are no longer reported as `is_latest=true`.
  Zenodo records take `is_latest` from Zenodo's own version flag, and resolving an older
  Zenodo version names the latest in `superseded_by`, so `relate` reports a
  `version_lineage` between two versions of one record.
- PDB search no longer drops structures published in the same paper: each entry carries
  its own DOI (`10.2210/pdbXXXX/pdb`) and the paper DOI moves to a `described_in` link.
  `resolve(trust=true)` also checks `described_in` paper DOIs, so a structure whose
  paper was retracted is still flagged.
- GBIF search results carry their publication year, so year filters no longer drop every
  GBIF dataset.
- GWAS Catalog and DANDI no longer return zero results when an organism or ontology
  filter is set; they receive the plain query, and the page says so.
- OpenAIRE papers no longer list the papers they cite as `datacite:` data links: only
  dataset and software targets are kept, labelled `datacite:` only when DataCite
  registered them.

### Changed

- `sra:SRP…` (study) and `sra:SRR…` (run) ids raise NotFoundError naming the
  experiment(s) to resolve, instead of returning the first experiment with the whole
  study's files. `sra:SRX…` is unchanged.
- A record whose links name only an older version or its concept now reports
  `is_latest=None` (unknown) rather than `true`; links can prove "superseded", never
  "latest".
- data.gov search `total` is a lower bound: the new catalog API reports no hit count.

### Security

- `operate` `sql`: a user query could still reach the network through the S3-family
  (`s3://`, `s3a://`, `s3n://`, `r2://`, `gcs://`, `gs://`) and HuggingFace (`hf://`)
  filesystems, including an arbitrary host via a per-URL `s3_endpoint` — the lockdown
  disabled only the local and HTTP filesystems. All external access is now switched off
  once the source is loaded, so every such read is refused.

## [0.46.1] - 2026-09-26

### Changed

- README and `docs/POSITIONING.md` claims now match the code: only the measured
  `understand=` recall result is reported (`multi_query=` is unmeasured); the original
  query's hits are always candidates but not guaranteed a place on the page; `fetch`
  verifies md5/SHA-256 only where a source publishes one; source tables regenerated.
- The `fetch` tool description names every fetchable source (UniProt, BioStudies, GBIF and
  data.gov were missing) and says which are unverified; a test now fails if one is left out.
- The personal contact email is removed from SECURITY.md, the Code of Conduct and the
  package metadata; a third-party email in a test fixture is redacted.

## [0.46.0] - 2026-09-23

### Fixed

Bug audit 2026-09-22 (H = high, M = medium, L = low). Each fix ships with a test that
failed on the old code for the stated reason.

- **A fetch that downloads nothing is an error, not a success (H1).** A restricted,
  embargoed or metadata-only record (`files=[]`), a `files` glob matching nothing, and a
  manifest with no fetchable entry all returned `paths=[]` with no error. They now raise
  `NotFoundError` / `ValidationError`, naming what was there. OpenNeuro GraphQL `errors[]`
  now raise instead of producing an empty manifest, and institutional Figshare DOIs
  (`10.25405/ncl.33951526.v1`) now list their files, after checking that the article's
  own DOI matches.
- **Files that share a basename no longer overwrite each other (H2).** Fetch cut every
  name down to its basename, so `hf:nyu-mll/glue`'s `cola/train.parquet` and
  `sst2/train.parquet` became one file. Relative directories are now kept (traversal
  segments are still removed). Names that are identical anyway, such as cellxgene's
  dataset-title names, get a stable `~<hash>` suffix.
- **An upstream failure no longer reads as "0 results" (H3).** These now raise:
  - NCBI's `esearchresult.ERROR` envelope, which arrives inside an HTTP 200 and was
    cached by taxonomy as "no match" for an hour.
  - A 404 or error-envelope body from the CELLxGENE, DANDI, OpenML, RCSB, GWAS Catalog
    and HuggingFace search endpoints, or from DANDI's asset listing.

  The shared checks live in `_http`: `expect=` for body shape and `check=` for in-band
  errors. Composite sources report each failing sub-database in `errors` by name
  (`omics/geo`, `literature/pubmed`, ...). Their own `search` raises when every backend
  fails.

- **Cursor walks return every record exactly once (H4, M5, M6).** Each source, and each
  sub-database of `omics` / `literature`, is now paged as its own stream with its own
  offset. The offset counts in the upstream's own order, and the cursor also carries the
  positions already returned past it (`ahead`). Before this:
  - omics and literature applied one offset to every sub-database: 60 of 90 records were
    never returned.
  - `rank=semantic` and multi-query consumed the whole fetched window but emitted only
    `size` records.
  - A DOI-deduped mirror, and records without a DOI (which dedup moved to the end of the
    window), shifted a source's offset, so later pages skipped or repeated records.
- **Continuation pages search the same query as page 1.** The cursor stored the raw
  query, so from page 2 on an `organism=` / `disease=` walk silently dropped the ontology
  restriction and paged a different, wider result set.
- **Keyword-only sources get the plain query (M9).** The boolean ontology expansion
  `(q) AND ("a" OR "b")` returned 0 hits from CELLxGENE, HuggingFace and OpenML and
  HTTP 400 from NASA CMR. These sources (the registry's `boolean_query=False`) are now
  sent the unexpanded query, and `errors["query_syntax"]` says the restriction was not
  applied there.
- **Zero hits are no longer reported as an outage (M10, M14).** Any 2xx is a success.
  RCSB's `204 No Content` and OpenML's `412` with code `372` ("No results") are an empty
  page; any other 412 is still an error.
- **A Dataverse DOI is looked up on its own installation (M11).** The server is now
  taken from the DataCite landing URL. Harvard is used only for `10.7910` DOIs. An
  unknown installation gets no file listing and a log line, not Harvard's 404.
- **DataONE packages with more than 50 data objects list all of them (M12).** The
  listing is paged, with a logged cap at 1000.
- **A nonexistent PMID raises `NotFoundError` (M13).** It used to resolve to an empty
  record, which was then cached.
- **The egress guard refuses carrier-NAT and Tailscale addresses (M15).**
  100.64.0.0/10 is neither private nor reserved to the stdlib, so the guard now also
  requires `ip.is_global`. IPv4-mapped IPv6 is judged as the IPv4 address it carries.
- **`.tsv` schema and preview split on tabs (M16).**
- **`doi:` / `https://doi.org/` / `http://dx.doi.org/` / `info:doi/` ids resolve (M8).**
  They reached DataCite verbatim and came back as a false NotFound.
- **GWAS Catalog and BioStudies search honour a mid-page offset (M7).** An offset that
  was not a multiple of `size` replayed rows the router had already consumed.
- **Smaller fixes (L17-L25):**
  - CSV/TSV preview drops the partial last line and sets `truncated` when the 64 KB
    sniff window cut the rows short.
  - `operate` rejects `n < 1`. The schema now declares `minimum: 1`.
  - `search(sources=[])` is a `ValidationError`, not an empty success.
  - Checksums compare case-insensitively: DataONE's `MD5:` / upper-case hex no longer
    fails a correct download.
  - Query understanding no longer raises on an LLM's `Infinity` / `NaN`.
  - The rate limiter works across event loops. It has per-loop locks and a fix for a
    float stall in the refill loop.
  - Scholix inverse relations keep their direction (`is_supplemented_by`,
    `is_referenced_by`).
  - A single-string OpenML `tag` is one subject, not a list of characters.
  - Every DOI placed in a URL path goes through one encoder, `_http.doi_path`. The
    doi.org citation, DataCite and Unpaywall requests used to send `#`/`?` raw, which
    cut the DOI short.
- **`test_all_tool_outputs_validate_against_schemas` can fail.** It used to construct
  the pydantic models and never looked at a schema. It now drives each tool's real
  handler and validates the output against the tool's declared `outputSchema`.

- **Elicitation resolvers are looked up at call time.** `_RESOLVERS` stored the
  resolver function objects at import, so the test suite's
  `monkeypatch.setattr(elicitation.taxonomy, "resolve_taxon", ...)` never bound:
  three elicitation tests called the live NCBI resolver from required CI and
  passed only while "yeast" stayed absent from the registry (the outage test
  passed because "mouse" resolved, never exercising the raise it was written
  for). Found by running the suite in an empty network namespace
  (`unshare -rn`); the whole suite now passes offline except the one
  deliberately live subprocess test.
- **`search` CLI: an upstream outage no longer prints `[]` with exit 0.** The
  router tolerates a failing source (so the others can still answer) and reports
  it in the page's `errors`; the CLI dropped that dict, making a Zenodo 504 storm
  indistinguishable from "no hits". Failed sources are now named on stderr, and
  exit 1 when every source failed. `test_search_cli_real_subprocess` derives its
  subprocess timeout from the code's own retry budget (30 s x 3 + backoff, +30 s
  slack) instead of a fixed 90 s that was _below_ that budget, which is why the
  2026-09-16 Zenodo outage surfaced as `TimeoutExpired` (a failure) rather than
  the non-zero exit the test skips on, and blocked merges on a required check.
  The test also now requires the two records it asked for.

### Changed

- **Migrated to the mcp 2.x low-level server API; the dependency is now `mcp>=2,<3`.**
  The seven handlers are `on_*` constructor arguments of `Server`, each taking
  `(ServerRequestContext, params)` and returning the typed result (`ListToolsResult`,
  `ReadResourceResult` with `TextResourceContents`, ...). `server.request_context` is
  gone: the fetch progress token and the elicitation session come from the `ctx` the SDK
  hands the handler (`ctx.meta["progress_token"]`, `ctx.session`, `ctx.request_id`), and
  `_dispatch` takes that context as an optional third argument. Wire-model fields are
  snake_case in 2.x (`Tool.input_schema`, `ToolAnnotations.read_only_hint`,
  `Resource.mime_type`, `CallToolResult.is_error`); the specs in `tool_specs.py` /
  `resources.py` are written that way now.

  Four things the 1.x `@server.call_tool()` decorator did silently are now done by our
  `on_call_tool` handler, because 2.x removed them and this server's contract depends on
  each: (1) arguments are validated against `inputSchema` (`"Input validation error: ..."`
  as an error result, same text as 1.x); (2) a dict result is returned as
  `structuredContent` plus its JSON as text — the 2.x _client_ raises `RuntimeError` when
  a tool with an `outputSchema` returns no structured content, so this is load-bearing for
  search/resolve/fetch; (3) the result is validated against `outputSchema`; (4) **every
  exception is returned as `CallToolResult(isError=True, content=[str(exc)])`**. Under
  2.x an exception that leaves the handler becomes a JSON-RPC _error_, and on a
  modern-era connection the SDK replaces its message with a generic "Internal server
  error" (`mcp/server/runner.py::modern_error_data`), so every refusal this server writes
  for the model (`FetchNotSupportedError`, `ValidationError`, ...) would have arrived
  textless. The stdio and streamable-HTTP entry-point smokes now call `fetch` on a
  non-fetchable id and assert the refusal TEXT in an `is_error` result, with `list_sources`
  succeeding in the same session as the positive control. Seen to fail: with the catch
  removed, both fail with `MCPError(0, "[FetchNotSupportedError] 'bioproject:PRJNA111'
has no wired fetch backend ...")`.

  Security floor: all three advisories the old floor named are patched at 1.27.2 /
  1.28.1 (GHSA-jpw9-pfvf-9f58 and GHSA-hvrp-rf83-w775 patched in 1.27.2;
  GHSA-vj7q-gjh5-988w in 1.28.1), so every 2.x release carries the fixes.
  `TransportSecuritySettings` and `StreamableHTTPSessionManager` are unchanged in 2.2.0;
  the live DNS-rebinding test still rejects a spoofed Host/Origin on the wire.
  `create_connected_server_and_client_session` no longer exists in 2.x; the end-to-end
  elicitation tests build the in-memory session the way the 2.x migration guide shows.
  The lockfile resolves mcp 2.2.0; the `declared-deps` job resolves the same.

### Fixed

- **The declared mcp range admitted a version this server cannot import.** #69 widened the
  ceiling from `<2` to `<3` a month ago. mcp 2.x removed the 1.x low-level decorator API
  this server is built on — `@server.list_tools()`, `@server.call_tool()`; handlers are
  constructor arguments there now — so `import data_aggregator_mcp.server` raises
  `AttributeError: 'Server' object has no attribute 'list_tools'` under it. Not a
  deprecation to work through later: the module does not load, so a lock-free install got
  a package whose server cannot start. The published 0.45.3 still carries `mcp<2` and is
  unaffected; anyone installing from source since 2026-08-04 was not.

  The ceiling is back at `<2`, with the reason next to it, and raising it is now the
  migration commit's job rather than a version-bump PR's.

  CI could not have caught this and still cannot in the jobs that existed: every one of
  them installs from `uv.lock`, so the declared range is never resolved and a wrong one
  cannot fail. The new `declared-deps` job installs from `pyproject.toml` alone and imports
  the server module — the smallest thing that fails when the range and the code disagree.
  Verified against the broken state: with `<3` it resolves mcp 2.1.1 and the import exits
  1; with `<2` it resolves 1.29.1 and exits 0.

- **A checksum we could not compute was ignored silently.** `fetch` verifies a declared
  checksum only when the algorithm is one `hashlib` provides. For anything else — DANDI's
  `dandi-etag`, or a typo — the download completed with no verification and **no signal**,
  indistinguishable from a record that declared no checksum at all. A caller who sees a
  checksum on the record reasonably reads that as a verified download.

  `FetchResult` now carries `unverified`, naming files whose record declared a checksum we
  could not honour, and a warning is logged when it happens. Records that declare no
  checksum are deliberately _not_ listed: that is already visible from `files[].checksum`,
  and listing every one would bury the case that actually misleads.

  Reported rather than raised on purpose. The algorithm is upstream's choice — `dataone`,
  `dryad` and `zenodo` all pass it straight through — so failing loud would turn working
  DANDI fetches into errors. Where the checksum _is_ computable, a mismatch still raises,
  exactly as before.

  This is an additive, defaulted field, so existing consumers are unaffected.

## [0.45.3] - 2026-07-28

### Fixed

- **A redirect bypassed the egress guard shipped in 0.45.2.** That guard validated the URL
  it was handed, but the client follows redirects — so a record with a perfectly _public_
  URL that 302s to `http://127.0.0.1:9200/` reached it with the check satisfied. Measured
  at the time: the guard was consulted once, about the entry URL, while the redirect target
  was fetched unchecked.

  Validation now runs as an httpx request event hook, which is the only layer that sees
  every address actually connected to. The call-site checks remain — they fail before any
  I/O and name the file, which a transport-level error cannot.

  0.45.2 does block a record pointing _directly_ at private space; it did not block the same
  thing behind a redirect. Anyone running `--transport http` should take 0.45.3.

## [0.45.2] - 2026-07-28

### Fixed

- **A record could make the server read addresses the caller cannot reach.** `operate` and
  `fetch` take the file URL straight from an upstream record, and every source that accepts
  uploads — Zenodo, HuggingFace, figshare, OpenML — lets that URL be anything. A record
  whose file is _named_ `data.csv` while pointing at `http://127.0.0.1:9200/` or
  `http://169.254.169.254/` was fetched, and with `op='head'` the body came back to the
  caller as rows.

  The v0.45.0 hardening does not cover this: `duckquery` disables the filesystems _after_
  materializing the source — necessarily, since a lazily-evaluated view would be blocked
  too — so that lock protects the user's SQL and structurally cannot protect the source
  read. The scheme allowlist governs _how_ we fetch, never _where_. And `_operable()` gates
  on the file's **name**, so the extension check never looked at the URL at all.

  A new egress guard resolves the host and refuses private, loopback, link-local, reserved,
  multicast and unspecified addresses before any request is made. Harmless over stdio,
  where the server is the caller's own child; the exposure was SSRF with response
  exfiltration under `--transport http`, where the server may sit in a network the caller
  cannot otherwise reach.

  Set `DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS=1` if you deliberately serve records from
  private address space. A host that does not resolve is still allowed — it points nowhere,
  so refusing it would block offline callers while closing nothing. This does not defeat
  DNS rebinding, which is documented rather than quietly implied.

## [0.45.1] - 2026-07-28

### Fixed

- **A hostile licence URL could mint a permissive verdict.** `host_matches` already
  rejected a domain sitting in someone else's path, but the scanner that feeds it splits a
  URL at any character it cannot consume — so one URL became two tokens and each half was
  read as a host. `http://creativecommons.org@evil.example.com/licenses/by/4.0/` returned a
  real `CC-BY-4.0` and an **ALLOW** for commercial use: the userinfo was read as the host.
  So did the `:8080@` port form, and `https://evil.example.com#creativecommons.org/…`,
  where the fragment matched. Open Data Commons URLs were affected the same way.

  This matters because licence strings are record data, and anyone can upload a record and
  set that field on Zenodo, HuggingFace or OpenML — so the verdict, the access flag and the
  FAIR score were all derivable from attacker-controlled text. A token is now rejected when
  the surrounding URL syntax proves it is not a host: followed by `@` (optionally via a
  port) means userinfo, and preceded by `@ # ? & =` means it sits inside another URL.

- **The same scanner was quadratic on attacker-supplied text.** Its host-label group nested
  a star inside a plus, so a 12 KB licence field of `by-by-by-…` cost ~1.5 s of CPU — and
  it runs three times per licence check, so a page of such records was minutes of wall
  clock. Every repetition is now bounded by DNS's own limits (label ≤ 63 chars, ≤ 8
  labels), and the scan is length-capped: 12 KB now costs ~12 ms. A test fails on a
  quadratic regression rather than merely on being slow.

## [0.45.0] - 2026-07-28

### Fixed

- **"Licence not stated" was also being said about records that stated one.** A record
  whose licence we could not parse got the same verdict reason as a record with no licence
  at all — while `license_raw` sat in the same response holding the value the message
  denied existed. The verdict (REVIEW) was right in both cases; the explanation was not,
  and it pointed the caller away from the one lead they had.

  OpenML made the cost concrete: it states `licence: 'Public'` on every dataset, so we
  answered "licence not stated" for a source that states something on every record.
  Unrecognized licences now quote the stated value — `licence stated as 'Public' but not
recognized` — which also surfaces the two known drops from the 17-source sweep,
  `other-open` (Zenodo) and `Springer TDM`.

  **No verdict changed and no licence is promoted.** `Public` stays REVIEW: "publicly
  available" is not "public domain", and mapping it to CC0 would invent a specific grant
  from a vague word. A test pins that so it is a decision rather than an oversight.

  The quoted excerpt is whitespace-collapsed and length-capped, since a licence field is
  arbitrary upstream text that can be a paragraph; the full value remains in `license_raw`.
  This is the same correction already applied to the versionless-CC branch, generalized —
  a stated-but-unusable licence should never be reported as silence.

### Added

- **The project is now formally citable.** `CITATION.cff` and `.zenodo.json` ship in the
  repository, so GitHub renders a "Cite this repository" entry and a Zenodo deposition can
  mint a DOI. `CITATION.cff` records the version in a fifth place independent of
  `pyproject.toml`, so CI now fails the build when the two disagree — a stale citation
  version is the kind of error nothing else would catch.

- **`cellxgene` now answers licence questions instead of reviewing them.** CZ CELLxGENE
  Discover publishes every dataset under CC-BY 4.0 as a condition of submission, but its
  curation API exposes no licence field anywhere — verified across all 386 published
  collections and their nested dataset objects, not a single sample — so every cellxgene
  record fell through to REVIEW "all-rights-reserved". The archive with the clearest
  blanket grant was among our most pessimistic answers. It now carries a source-level
  default, applied only where the record itself is silent, with the policy cited.

  A live test asserts the premise rather than trusting it: if CZI ever adds a licence
  field, the test fails loudly, because at that point a record-stated licence starts
  winning and the blanket default needs re-examining.

### Changed

- **Four sources are now documented as deliberately having no blanket licence.** Auditing
  the remaining licence-less sources found only one qualified, and the reasons the others
  failed are worth recording in the registry so a future coverage push doesn't "fix" them:
  - `dataone` and `omicsdi` federate other repositories, so the terms belong to the member
    repo, not the aggregator. DataONE's Solr index has no licence field at all — the query
    returns "undefined field" — only `rightsHolder`, which is populated on 100% of ~3.36M
    records but names the rights _holder_, not a grant.
  - `omics` (NCBI) and `biostudies` (EMBL-EBI) publish near-identical careful wording: they
    place no _additional_ restrictions beyond the original data owner's. NCBI goes further
    and states that because "there is no transfer of rights from submitters to NCBI, NCBI
    has no rights to transfer to a third party".

  "No additional restrictions" is not permission. Defaulting a licence for these would
  invent a grant the operator explicitly declined to make. A test pins each of them absent.

## [0.44.0] - 2026-07-28

### Added

- **Creative Commons 1.0/2.0/2.5/3.0 are now assessed, not merely identified.** The
  normalizer already recognized `CC BY 3.0`, but only the 4.0 family carried
  compatibility flags, so a licence that plainly permits commercial use returned
  REVIEW — "identified as CC-BY-3.0, but no compatibility profile is bundled" — while
  the identical 4.0 licence returned ALLOW. That is the same conservative-but-misleading
  answer that source-level blanket licences rejected, and it covered **24 SPDX ids**
  (6 CC families × 4 pre-4.0 versions), the versions that dominate older repository
  records. All 24 are now hand-encoded from the licence texts, following the
  OGL-UK-3.0 precedent.

  The grants a CC family makes did not change across versions — BY permits commercial
  use, modification, distribution; NC removes commercial use; ND removes modification;
  SA adds share-alike — so the verdicts match their 4.0 counterparts on every intent,
  and that equivalence is asserted per family and version rather than assumed.

  The profiles are **not** copies of the 4.0 ones. Pre-4.0 CC says nothing about
  patents, and its only trademark clause disclaims _Creative Commons'_ own marks
  rather than the licensor's; the explicit "Patent and trademark rights are not
  licensed under this Public License" sentence arrived in 4.0. So `patent-use` and
  `trademark-use` are omitted here — silence is not an explicit exclusion, the same
  rule already applied to OGL-UK-3.0. Since verdicts derive from permissions alone,
  this is reported accurately without changing any answer.

  The real 3.0-vs-4.0 differences — attribution mechanics and the DRM-circumvention
  clause — fall outside the modeled flag vocabulary, so the approximation holds only
  for the current intent set. A guard test pins that set: adding an intent which turns
  on either area fails loudly instead of silently mis-answering.

- **The UK Open Government Licence v1.0 and v2.0 are now assessed too, closing the
  identified-but-unassessed set entirely.** The URL normalizer already yielded
  `OGL-UK-1.0` and `OGL-UK-2.0`, but only v3.0 carried a profile, so two versions of a
  licence whose whole purpose is unrestricted public-sector reuse answered REVIEW. All
  three versions grant the same shape — copy, publish, distribute, adapt, and exploit
  commercially, conditioned only on attribution — so they now share one hand-encoded
  profile verified against the legal text of each version. The prose and short-code
  forms (`OGL v2.0`, `Open Government Licence 2.0`, …) are recognized for v1.0 and v2.0
  as well, which previously only worked for v3.0.

  Bare `OGL` with no version stays deliberately unrecognized. The verdict would now be
  the same for any version, but `spdx_id` is the field callers cite, and naming a
  specific version for a source that never stated one invents a fact.

  With this, **every licence id the normalizer can emit also carries a compatibility
  profile**, and a new invariant test pins that. The failure it guards is silent: adding
  an alias or URL pattern without a matching profile downgrades a plainly-stated licence
  to REVIEW, which reads as caution rather than as the gap it is.

### Fixed

- **`OGL-UK-3.0` wrongly reported that the licence is silent on patents.** Its profile
  omitted `patent-use` on the stated grounds that "patents are not addressed", but every
  OGL version's exemption list explicitly carves out "other intellectual property rights,
  including patents, trade marks, and design rights". The licence text is not silent, so
  the "silence is not an explicit exclusion" rule never applied here. `patent-use` is now
  asserted for all three versions. Verdicts are unaffected — they derive from permissions
  alone — but the reported limitations were misdescribing the licence. A test had encoded
  the same mistaken claim; it was corrected against the licence text rather than relaxed.

  This is also why OGL and pre-4.0 CC land on opposite answers for `patent-use` despite
  sharing an encoding rule: the OGL texts exclude patents explicitly, pre-4.0 CC does not
  mention them at all.

## [0.43.0] - 2026-07-27

### Added

- **Source-level blanket licences, for sources that publish one.** Measuring the
  licence string every source actually returns found that the real gap is not
  misparsing but absence: 10 of 17 sources state no licence at all, and `resolve`
  confirms it is genuinely absent rather than omitted at search time. Where the
  operator dedicates the entire archive under a single licence, answering
  "defaults to all-rights-reserved" is a wrong answer rather than a safe one.
  `resolve(use=…)` now falls back to that policy when — and only when — the record
  itself is silent. `pdb` is `CC0-1.0` (the wwPDB dedicates the archive; entries
  carry no licence field at all) and `uniprot` is `CC-BY-4.0` (every UniProtKB
  flat-file record states it in-band, a notice the JSON our adapter reads drops).
  The verdict says where the licence came from and leaves `license_raw` as `None`,
  because the _record_ still said nothing. A licence on the record always wins.
  `gwas` is deliberately excluded despite being mostly CC0: individual studies
  carry their own Usage License, so a blanket default would be wrong precisely
  where it matters. Every declared default must be a canonical SPDX id present in
  the compatibility matrix and must carry its policy citation, enforced at test
  time — an unsourced guess about someone else's data is worse than "unknown".

- **The README now documents the streamable HTTP transport.** `--transport http`
  and every flag that goes with it (`--host`, `--port`, `--allow-host`,
  `--allow-origin`, `--stateless`, `--json-response`), the `/mcp/` endpoint and
  the `307` that `/mcp` redirects with, the always-on DNS-rebinding rules —
  including the deliberate refusal to start on a non-loopback bind without an
  explicit `--allow-host`, and the `421` a forged `Host` header earns — and that
  `fetch(dest=…)` writes to the **server's** filesystem, not the caller's. The
  transport itself shipped in 0.42.0, but appeared nowhere someone installing the
  package would look: the README is also what PyPI renders, so the feature was
  effectively undiscoverable.
- **Four environment variables the code reads but nothing documented**:
  `DATA_GOV_API_KEY`, `NCBI_EMAIL`, `DATAVERSE_BASE_URL`, and `CACHE_TTL_SECONDS`
  are now in the README configuration list, and the latter three are declared in
  `server.json` so the registry entry lists them too. `DATA_GOV_API_KEY` mattered
  most: without it the data.gov source silently shares the public `DEMO_KEY`, which
  is capped around 30 requests/hour per IP.

### Changed

- The configuration section said "Both optional" while listing four groups of
  variables.

### Fixed

- **NCBI pacing now follows the request host, not the service label.** The rate
  limiter picked its bucket with `service.startswith("NCBI")`, so GEO's
  supplementary-file listing — which fetches from `ftp.ncbi.nlm.nih.gov` under the
  label `GEO suppl listing` — drew from the default bucket at 10 req/s, over three
  times NCBI's keyless ceiling, purely because of what the call was named. NCBI
  throttles per account/IP across its hosts, so the host is the thing to key on; the
  label survives only as a backstop for a URL that cannot be parsed, where it can add
  pacing but never remove it. Honest scope note: this was found while triaging an
  intermittent `GEO suppl listing → HTTP 403`, and it does **not** demonstrably fix
  that — the 403 never reproduced in isolation (the exact URL returned 200 ten times,
  the test passed 3/3, and 12 concurrent requests to that host all succeeded). It
  appeared only under whole-suite aggregate NCBI load. The pacing gap is a real defect
  on its own terms; whether it causes that 403 is unproven.

- **A Creative Commons licence stated without a version is no longer discarded.**
  EuropePMC states its licence as `cc by`, `cc by-nc-nd` and friends — never with a
  version. Across 300 sampled open-access records, 231 carried such a string and not
  one carried a version, and `normalize_spdx` returned `None` for every one of them,
  so the largest licence-bearing path in the product answered `licence not stated /
not recognized; defaults to all-rights-reserved` for licences that were plainly
  stated. `check` now identifies the family and says what it actually knows —
  `licence stated as CC-BY-NC but with no version; the version determines the terms,
so no compatibility profile can be selected`. `spdx_id` stays `None`, because there
  is no SPDX id for a versionless CC licence, and no version is guessed: 3.0 and 4.0
  differ on attribution and on the effect of a DRM clause, so picking one would be the
  fabrication this module exists to refuse. New `identify_cc_family` is the third
  identification outcome the module lacked — not "unknown", not "identified
  precisely", but "family known, version not".

## [0.42.0] - 2026-07-25

### Security

- **`operate` with `op='sql'` can no longer make the server fetch arbitrary URLs.** The
  DuckDB engine disabled the local filesystem after materializing the source but left
  `httpfs` loaded, so a crafted `SELECT * FROM read_csv_auto('http://…')` caused the
  **server** to issue that request and return the response body as query rows. Over stdio
  this is largely invisible — the server is the caller's own child process — but under
  `--transport http` the server can sit in a network the caller cannot otherwise reach,
  making it server-side request forgery with the response exfiltrated through the result
  set (cloud instance-metadata endpoints, internal services). `HTTPFileSystem` is now
  disabled alongside `LocalFileSystem` before the configuration is locked; this costs
  nothing because the source is already materialized in memory by that point. Reported by
  an internal security audit; covered by a regression test that drives a real listener and
  asserts the socket is never contacted.

### Fixed

- **Licence ids qualified with an `spdx:` scheme prefix are now recognized.**
  `spdx:CC-BY-4.0` normalized to _unknown_ while the bare `CC-BY-4.0` normalized fine, so
  every DANDI record reported no licence at all and its compatibility verdicts silently
  degraded to unknown.

- **`bioproject:` ids can now be resolved at all.** NCBI's BioProject index has no `ACCN`
  field, but `resolve` built the same `<acc>[ACCN]` term for all three omics databases — so
  every BioProject accession matched zero records and raised `NotFoundError`, while the
  identical syntax was correct for GEO and SRA. Since `search` surfaces `bioproject:` ids,
  a third of omics results were unresolvable dead ends. The accession field is now declared
  per database. Fixing that exposed a second failure directly behind it: a BioProject's SRA
  links come from an unbounded `elink`, and feeding thousands of uids into one `esummary`
  request overflowed the URL length limit outright (`PRJNA231221` links 7,314 runs). Linked
  runs are now capped at 100 and the truncation is logged rather than silent.

- **Open-access full text is no longer lost whenever a PMCID is known.** EuropePMC's
  `PMCID` field does not match a phrase-quoted value — `PMCID:"PMC3463246"` returns zero
  hits where `PMCID:PMC3463246` returns the record — and because callers pass the PMCID
  ahead of the DOI, the quoting silently beat a DOI lookup that would have worked. A
  resolved paper therefore came back with no files and no access, and `fetch` reported "no
  open-access full text (it may be paywalled)" for papers that are open access and whose
  full text EuropePMC serves. The PMCID is now whitelisted against `PMC<digits>` and
  interpolated bare, which is a stricter injection guard than quoting was; DOIs still
  phrase-quote, as that field matches fine quoted.

- **OpenNeuro datasets get their file manifest again.** The snapshot query declared
  `$ds: String!` while OpenNeuro's schema is `snapshot(datasetId: ID!)`. GraphQL rejects the
  entire document on that mismatch, so every lookup returned HTTP 400 and no OpenNeuro
  dataset ever resolved with files.

- **Every id advertised by `list_sources` now resolves.** Four of the twenty advertised
  example ids could not be resolved: `datacite:10.5061/dryad.x` and `cellxgene:col-lung-1`
  were placeholders (the first indistinguishable from a real DOI), and the literature entry
  shipped a literal `openaire:<id>`. `list_sources` is how a model discovers what to call,
  so each is replaced with a real id verified to resolve, and a test now rejects placeholder
  tokens and unroutable prefixes.

- **A malformed pagination cursor whose `q` is not a string is now rejected up front.**
  `q` was the one required cursor field whose type went unvalidated, so a cursor carrying
  `q: null` passed decoding, fanned out a `None` query to every selected source over the
  network, and only failed afterwards inside `SearchResult`'s own validation — paying the
  full request cost for a guaranteed error and reporting it as a schema failure rather
  than a bad cursor. Surfaced by typing the adapter contract (below).

- **`uniprot:` records can now be resolved and fetched.** UniProtKB was registered and
  advertised as fetchable, but had no branch in `resolve()`'s prefix-routing chain, so
  `resolve`/`fetch` on a `uniprot:` id fell through to a `ValueError` (search worked, but the
  result was a dead end). Added the routing branch, plus a drift-guard test asserting every
  registered adapter that declares `PREFIXES` is routable. Found by an organization audit.

- **`_dedup` no longer lets a discovery-only source shadow the fetchable copy of a dataset.**
  On a shared DOI, precedence now keys on real fetchability (fetchable native > DataCite >
  discovery-only) instead of the `datacite:`-prefix proxy — so a record from a discovery-only
  source (NASA CMR, GWAS) can no longer win by mere interleave position and drop the
  checksum-verified copy (e.g. an ORNL DAAC DOI indexed by both CMR and DataONE). Found by a
  bug audit of this cycle's new sources. Also hardened the new NASA CMR and data.gov adapters
  against schema-violating responses (non-string licence leaves and null list elements no
  longer crash a whole search leg; a prefix-only `doi:` yields `None`; CKAN string-form
  resource sizes are coerced instead of dropped), and completed the `search` `sources` /
  `list_sources` docs for the sources added this cycle.

### Changed

- **Two error messages state less than they used to claim.** The "no open-access full text"
  failure listed only causes outside our control ("it may be paywalled, or not in
  EuropePMC/Unpaywall") — the framing that made a broken PMCID lookup read as a property of
  the _paper_; it now also allows that the lookup itself may have failed. And `operate`'s
  rejected-scheme message no longer names the environment variable that turns the `file://`
  restriction off: a user-facing error should not coach the caller into disabling a security
  control (the escape hatch stays documented in the source, for operators).

- **`router.py` is now just the orchestrator.** Two self-contained policies it only
  sequenced were embedded in it: cross-source record identity (exact-DOI dedup + mirror
  collapse) and ontology query expansion. They now live in `_mirror` and `_ontology`,
  which the router imports one-way — neither imports back. `router.py` drops from 1261 to
  ~880 lines, and the merge policy can be read and tested without wading through the
  fan-out. No behavior change: every moved function is AST-identical to its original, and
  the router re-exports each under its historical private name, so existing callers and
  tests address live code rather than a copy.

- **The source-adapter contract is now a checked `typing.Protocol` (`SourceAdapter`).**
  Adapters are modules rather than classes, and the registry typed each one as `Any` — so a
  module registered without `resolve`, or with a signature that had drifted, type-checked
  cleanly and only failed at runtime on whichever id happened to route to it. That is the
  same latent shape as the `uniprot` bug. Registering an incomplete module is now a type
  error **at the registry call itself**, which is exactly where a new source gets added.
- **Fewer redundant round-trips and handshakes on the hot paths.** Three independent
  efficiency fixes from the same audit, none of which change any result:
  - A serve session (stdio _and_ streamable HTTP) now owns **one HTTP client for its whole
    lifetime** instead of building one per tool call, so sequential calls — paging, resolve
    -after-search — reuse pooled connections rather than repeating DNS + TLS to every host.
    Callers with no serve session (unit tests, the one-shot `search` CLI) keep the previous
    per-call lifecycle.
  - **Resolving a DOI in Zenodo's own `10.5281` namespace no longer calls DataCite first.**
    That record was always re-resolved against the native Zenodo API, so the DataCite response
    was fetched only to be discarded. Zenodo records minted under any other prefix are
    unknowable without asking DataCite and still take the fetch-then-delegate path — the
    change is to cost, not coverage.
  - **Semantic re-rank computes the query norm once** per ranking instead of once per
    candidate (it runs on every `rank=semantic` page and every multi-query merge). Verified
    rank-for-rank identical to the previous implementation across 4000 randomized trials,
    including zero-norm vectors and exact ties.

- **Introduced a central source registry (`sources.py`).** Per-source routing and
  fetchability were previously restated in four places kept in lockstep only by tests (the
  gap that produced the `uniprot` resolve bug). They now derive from one ordered registry:
  `router`'s adapter map, the discovery-only set, `server`'s fetch gate, and the `resolve`
  dispatch (now a registry loop instead of a hand-written 16-branch chain) all read from it,
  so a new source is one row. Internal refactor — no behavior change; the derived
  adapter/gate/discovery sets are byte-for-byte the previous ones, and dispatch is verified
  live. `zenodo`/`datacite` gained `PREFIXES` (their bare-id routes stay as explicit
  fallbacks).

- **The `list_sources` catalog now lives in the registry too, one row per source.** The
  ~265-line `_SOURCES` metadata table moved out of `server.py` (1207 → 945 lines) onto the
  registry entries, so a source's advertised description and its routing/fetch behavior are
  declared together. `fetchable` became a _single_ declaration doing both jobs — it gates
  fetch and is the label the catalog advertises (`False` = discovery-only, `True` = every
  prefix fetchable, a string like `"per-repo"` = fetchable but decided per record) — so the
  advertised capability and the actual gate can no longer disagree; a contradiction now fails
  loud at import instead of misleading a client. Internal refactor — no behavior change: the
  `list_sources` payload is byte-for-byte identical, key order and sparse optional keys
  included, verified against a pre-refactor capture through the real tool dispatch.

- **Static MCP wire specs moved to `tool_specs.py`.** The tool JSON schemas, the prompt
  catalog and the prompt-text templates left `server.py`, which drops to 468 lines (from
  1207 before this cycle) and is now request-handling logic rather than mostly literal.
  `server` re-exports them under their historical names, so `server.TOOLS` / `server._PROMPTS`
  keep working. Internal refactor — no behavior change: tool and prompt payloads are
  byte-for-byte identical, verified by speaking MCP over stdio to the real server process
  (`initialize` / `tools/list` / `prompts/list` / `prompts/get`), not just by importing the
  module. Schema defaults still read from the modules that enforce them (`zenodo` page sizes,
  the `fetch` byte ceiling), now pinned by a test so an advertised default cannot drift from
  the applied limit.

- **A `resolve` that follows a `search` on Zenodo no longer re-fetches the record.** Zenodo
  search already returns the full record (with the file manifest), so `search` now stashes the
  raw record in a short-TTL cache and `resolve` serves it without a second `GET` — one fewer
  upstream round-trip on the common search→resolve flow. Also consolidated the id-prefix-strip
  idiom that had drifted across ~14 adapters into a single `models.local_id` helper.

- **Per-search organism enrichment now runs concurrently instead of serially.** Taxon
  resolution across a result page is `asyncio.gather`-ed (bounded by the shared rate limiter
  and the resolver cache), cutting cold-page latency ~2-3× when an `NCBI_API_KEY` is set; a
  taxonomy failure is still recorded in `errors['taxonomy']` but no longer aborts enrichment
  of the rest of the page. Consolidated the copy-pasted `_year` (×4) and HTML-strip (×2)
  adapter helpers into shared `models.year_from` / `models.strip_html`.

### Added

- **Recognize the UK Open Government Licence v3.0 (`OGL-UK-3.0`).** `normalize_spdx` now
  maps the bare SPDX id, the prose / short-code forms (`OGL 3.0`, `OGL3`, `Open Government
Licence 3.0`), and the nationalarchives.gov.uk OGL URL to `OGL-UK-3.0` (the v1/v2 URLs are
  identified too); a bundled compatibility profile — hand-encoded from the licence text —
  assesses it as a permissive attribution licence (commercial use ALLOWed, attribution
  required, no trademark grant, patents unaddressed). Previously every OGL form returned
  `None` (unrecognized → REVIEW). Bare `OGL` with no version stays unmapped (ambiguous).
- **NASA CMR (Earthdata) wired as a source (S3).** The Common Metadata Repository's
  earth-science collection catalog (satellite, atmospheric, oceanographic, climate) —
  `search` + `resolve` via the keyless UMM-C JSON API. This is a **discovery-only** leg:
  a CMR collection has no single downloadable file (granule bytes live behind an Earthdata
  login this server does not wire), so `files` is empty and `fetch` is not offered — the
  same shape as the GWAS Catalog leg. `resolve` carries the DOI, provider, science
  keywords, and an Earthdata Search data-access link. Many collections carry a real DOI
  (`10.5067/…`), so these records do participate in cross-source DOI dedup.
- **data.gov wired as a source (S3).** The US government open-data catalog: `search` +
  `resolve` cover CKAN packages (climate, agriculture, economic, civic & scientific
  data), each with a publishing agency, tags, a (frequently unspecified) licence, and
  downloadable resources that `fetch` streams. data.gov **retired its keyless CKAN API**
  in favour of a GSA-hosted Catalog API that requires a free api.data.gov key; the key is
  read from `DATA_GOV_API_KEY` (mirroring the optional `NCBI_API_KEY` pattern) and sent as
  `X-Api-Key`, falling back to the rate-limited public `DEMO_KEY` for light use. Fetch is
  **unverified** — CKAN rarely exposes a usable checksum — so the content sniff guards but
  no hash is checked; metadata-only packages are discovery-only and fail loud. Most
  packages carry no DOI, so this leg is breadth, not cross-source dedup.
- **GBIF (Global Biodiversity Information Facility) wired as a source (S3).** The unit
  is a GBIF _dataset_ (occurrence / checklist / sampling-event / metadata): `search`
  and `resolve` cover the registry, and each dataset carries a DOI, a machine-readable
  licence (normalized to SPDX), and — for archive-backed types — a downloadable Darwin
  Core Archive that `fetch` streams. The archive fetch is **unverified**: GBIF publishes
  no checksum, so the fail-loud content sniff still rejects an HTML error page served as
  a zip, but no hash is checked (the HuggingFace precedent). Metadata-only datasets are
  discovery-only and fail loud on fetch. Registered _before_ DataCite in the router
  because GBIF DOIs use the `10.15468` DataCite prefix and both index them —
  native-before-datacite makes the fetchable GBIF record win the DOI-dedup collision.
  This is the first source outside the molecular-omics / archive core, extending
  coverage into biodiversity and natural-history data.
- **`search` reports ontology params that matched nothing, in `unresolved[]`.**
  The five ontology-typed params (`organism`/`disease`/`tissue`/`chemical`/`assay`)
  are looked up in NCBI Taxonomy / MeSH / UBERON / ChEBI / EDAM, and a lookup that
  found no term previously left the query un-expanded with _nothing recorded_ — so
  `search(query="liver cancer", organism="yeast")` returned a page byte-identical to
  one where the param was never passed, and the caller could not tell their filter
  had been dropped. This is common, not exotic: a live probe (2026-07-23) found NCBI
  Taxonomy indexes none of `yeast`, `oak`, `cedar` or `bass`, and UBERON has no bare
  `root`, `bulb` or `body`. Kept disjoint from `errors[]`, which continues to mean
  the lookup _failed_ rather than legitimately returned no match. Frozen to `[]` on a
  cursor continuation, alongside the other page-1-only echoes.
- **Elicitation (S1.6): clients that support form mode are asked to fix an
  unresolvable ontology term before the search runs.** One form covers every
  unresolved field; a blank answer means "search without it". Runs as a pre-flight
  rather than a retry, so it costs no extra upstream request (the resolvers are
  in-process cached) and never a second fan-out. Fail-soft at every rung — no
  session, no capability, a URL-only client, a transport error, or a user who
  declines all leave the search exactly as it is today.

  Two things it deliberately does **not** do, both settled by live probe rather than
  assumption: it does not offer a "did you mean …" candidate list (relaxed lookups
  return usable suggestions for EDAM but noise elsewhere — `sugar` →
  _sugar-phosphodiester opine_, `root` → _ventral root of spinal cord_ — with no
  reliable way to tell the two apart), and it does not prompt on genuinely ambiguous
  terms (only 2 of 20 probed terms had multiple exact-match candidates, and the
  existing top-hit heuristic picked correctly in both, so a prompt would interrupt
  the user to re-ask a question the server already answers right).

  Capability detection deliberately does not use the SDK's `check_client_capability`,
  which stops at `elicitation is None` (mcp 1.28.1 `server/session.py:153`) and would
  report a URL-only client as form-capable.

- **PDB `resolve` now returns creators, source taxa, and funding.** From the RCSB
  GraphQL entry: `audit_author` (ordered), `rcsb_entity_source_organism` across all
  polymer entities (deduped by taxid), and `pdbx_audit_support` (only when a funding
  organization is actually named). Re-verified live 2026-07-22 — `4HHB` →
  Fermi/Perutz + `9606 Homo sapiens`; `6VXX` → 7 authors + `NIH/NIGMS GM120553`.
  Entries with no upstream funding record stay empty rather than fabricating one.
- **OmicsDI `resolve` now returns the depositor, free-text organism, and PMID/DOI.**
  Creators come from `submitter`/`submitter_name` — the **depositor**, deliberately
  not the publication's authors — and `pmid`/`doi` are parsed out of
  `additional.publication`. Organism stays **free text** rather than structured taxa
  because the upstream `cross_references.TAXONOMY` list mixes unnamed entries and
  contaminants; a live record returning `['sea water', 'blank sample']` is exactly
  why. Re-verified live 2026-07-22 (`pride:PXD002213`,
  `metabolights_dataset:MTBLS14508`).
- **Zenodo `search`/`resolve` populate `Metrics(views, downloads)`** from the record
  `stats` block. Verified live.
- **`relate` canonicalizes known landing-page URLs to `source:id`** before matching,
  so a link or version-lineage hint pointing at `zenodo.org/records/456` now joins
  `zenodo:456`. Covers zenodo, pubmed, geo, sra, bioproject, gwas, openml, pdb,
  dandi, and owner-namespaced HuggingFace datasets; an unrecognized URL passes
  through unchanged, so a miss is a false negative and never a wrong hint.
- **`list_sources` exposes `semantic_rank_available`** so a client can tell whether
  `rank=semantic` will actually re-rank. Pure environment read, no network.
- **`QueryUnderstanding` echoes an advisory, uncalibrated `confidence`.** Explicitly
  not a gate: confidence alone never changes a rewrite.

### Changed

- **Licence identification is no longer gated on the compatibility matrix.**
  `normalize_spdx` built the correct SPDX id and then discarded it unless the
  licence was in `LICENSE_MATRIX`, which carries only the CC **4.0** line — so
  `CC-BY-SA-3.0` came back as "unrecognized", and the provenance dossier printed
  `unrecognized` for a licence the code had just named. Identity now follows
  Creative Commons' own rules (the six combinations CC issues; BY required; ND
  and SA mutually exclusive), independent of which licences we hold flags for.
  `check` still answers **REVIEW** when no profile is bundled — but now reports
  the id and says why: "licence identified as CC-BY-SA-3.0, but no compatibility
  profile is bundled for it". No flags are invented for unbundled licences.

### Fixed

- **CC versions 1.0 / 2.0 / 2.5 / 3.0 were unidentifiable, and the prose form
  built a malformed id.** The URL and prose paths each carried their own version
  regex; both accepted only `[1-4].0` (so `2.5` matched neither), and they
  disagreed on whether the captured group already included the `.0` — leaving
  `"CC BY 3.0"` to produce `CC-BY-3.0.0`. Both now derive from one definition of
  the versions CC actually published. Seen live: a real record carrying
  `cc-by-3.0` now identifies as `CC-BY-3.0`.

- **Licence domain checks keyed on a substring, so a licence could be spoofed by
  URL path.** `normalize_spdx` accepted
  `http://evil.example.com/creativecommons.org/licenses/by/4.0/` as `CC-BY-4.0`,
  and DataCite's `_access_from_rights` read
  `http://paywall.example.com/creativecommons.org` as `access="open"` — both from
  a bare `"creativecommons.org" in uri` test. Upstream `rightsUri` values are not
  ours, so a malformed or hostile one could mint a permissive verdict that then
  feeds the compatibility matrix, the access flag, and the FAIR score. Both now
  compare the parsed URL **host** (`license_compat.host_matches`), which also
  rejects the suffix trick `creativecommons.org.evil.com`. Prose, bare SPDX ids,
  scheme-less `creativecommons.org/publicdomain/zero/1.0`, subdomains, and
  prose-with-an-embedded-URL all still normalize as before.

### Added

- **Streamable HTTP transport** (`--transport http`). The same six tools, prompts,
  and resources now serve over HTTP as well as stdio; both share one `Server`
  object and every handler, so there is no second code path to drift. Costs no new
  dependencies — starlette and uvicorn already ship transitively with `mcp`.
  Options: `--host`/`--port` (default `127.0.0.1:8000`), `--allow-host`,
  `--allow-origin`, `--stateless`, `--json-response`. Bare invocation still serves
  stdio, unchanged.
- **DNS-rebinding protection is always on for HTTP, and refuses to guess.** The SDK
  middleware silently disables Host/Origin validation when handed no settings, so
  the transport never passes none. A loopback bind derives its own allowlist; a
  non-loopback bind (`--host 0.0.0.0`, a LAN or container address) **requires** an
  explicit `--allow-host` and exits 2 with an actionable message otherwise. Covered
  by wire-level tests that assert a spoofed `Host` gets 421 and a spoofed `Origin`
  gets 403 against a real server on a real port.
- **BioStudies (EBI) connector**, including the **ArrayExpress** collection — EBI's
  counterpart to GEO, and previously reachable only indirectly through OmicsDI.
  `search` is free-text across collections (or narrowed with `collection=`) and
  pages by page number; `resolve` returns the file manifest, the publication DOI,
  and sibling accessions.
- **Cross-references feed `relate` and DOI dedup.** A BioStudies study's sibling
  accessions (GEO `GSE…`, ENA `PRJ…`) land in `accessions`, so resolving
  `biostudies:E-GEOD-30436` alongside `geo:GSE30436` now yields a
  `shared_accession` hint naming `GSE30436` as the evidence — a cross-repository
  link the router could not previously make. Promotion is allowlisted by link
  type, because `relate` reports an accession as hard evidence and a junk value
  would manufacture a false connection.

### Notes

- **BioStudies fetch is UNVERIFIED and the catalog says so.** The API publishes no
  md5/sha256 for study files (checked against the live payload), so
  `FileEntry.checksum` is None. `fetch` still streams and still fails loud on an
  HTML error body; it simply cannot make the integrity claim a Zenodo or ENA fetch
  makes. A test asserts the absence, so if BioStudies ever adds checksums the
  catalog note gets revisited rather than silently going stale.
- **`totalHits` is an estimate on the cross-collection search** (the API returns
  `isTotalHitsExact: false`; consecutive pages reported 1549 then 1550). It is
  exact within a single collection. The number is passed through as given rather
  than implying a precision the source does not have.

### Fixed

- **`initialize()` reported the MCP SDK's version as the server's.** `Server` was
  constructed without `version=`, and the SDK falls back to its own version in that
  case — so clients were told `data-aggregator-mcp` was at `1.28.1`. It now reports
  `__version__` (0.41.1). Affected **both** transports; caught while verifying the
  HTTP handshake, and now guarded by a test.

### Security

- **`mcp` pinned to `>=1.28.1,<2`** (was the unbounded `>=1.0`), clearing three
  high-severity advisories: GHSA-vj7q-gjh5-988w (WebSocket transport skips
  Host/Origin validation), GHSA-jpw9-pfvf-9f58 (HTTP transports serve session
  requests without verifying the authenticated principal), and GHSA-hvrp-rf83-w775
  (task handlers let any client access or cancel another's tasks). The first two
  are directly reachable from the transport added above, so the bump lands in the
  same change rather than after it. The ceiling keeps us off the 2.0 line until its
  breaking changes are evaluated.

### Changed

- **Docs corrected against the shipped v0.41.1 surface.** A competitor/gap audit
  found the public docs describing an older product: `docs/POSITIONING.md` still
  claimed "four tools" and a v0.18.0 source list, and listed `operate`, MCP
  resources, and semantic rank as _unshipped gaps_ when all three are live. It is
  rewritten against the live code — six tools, 13 sources, prompts + resources,
  a 2026-07-21 competitor sweep, and an honest-gaps section that now names the
  real ones (no Streamable HTTP transport, no MCP Tasks extension, no
  BioStudies/ClinicalTrials.gov/GDC/ENCODE, ~90% bio wiring).
- **Source count 12 → 14** in `README.md` and the `server.json` registry
  description — UniProtKB landed in 0.41.0 but was never added to either
  headline, and BioStudies adds the fourteenth.
- README now surfaces the FAIR score and `resolve(format="provenance")` dossier,
  which the 2026-07-21 sweep found no other MCP server pairs with
  checksum-verified fetch.

## [0.41.1] - 2026-07-03

### Fixed

- **Package `__version__` was left at 0.40.0 in the 0.41.0 release**, so the module
  attribute (and the version stamped into generated RO-Crate/dossier provenance
  metadata) disagreed with the distribution version. All four version sources
  (`__init__.py`, `pyproject.toml`, and both `server.json` fields) are now synced to
  0.41.1; the `test_version_is_synced_across_all_sources` guard passes.

## [0.41.0] - 2026-07-03

### Added

- **UniProtKB connector** (#15) — UniProtKB is now a first-class default source:
  full-text search reads the accurate `x-total-results` header (cursor-paginated,
  so like huggingface it contributes to page 1 only — `offset>0` returns no rows),
  `resolve` attaches a FASTA `FileEntry` (unverified — no upstream checksum), and an
  injection-safe accession guard fails before any network call.
- **`search --json` one-shot CLI subcommand** (#16) — an explicit `search` first-arg
  diverts to a one-shot CLI that prints the `SearchResult.results` array as JSON,
  enabling lightweight non-MCP consumers (e.g. recap's DaProvider). Bare invocation
  still starts the MCP server unchanged.

### Fixed

- Registry `server.json` description now fits the MCP registry's 100-char limit,
  with a guard test to keep it there.

### Changed

- Dependency bumps: starlette 1.1.0→1.3.1 (#10), aiohttp 3.14.0→3.14.1 (#11),
  cryptography 48.0.0→48.0.1 (#12), pydantic-settings 2.14.1→2.14.2 (#13),
  python-multipart 0.0.29→0.0.31 (#9), actions/checkout 6.0.3→7.0.0 (#14).
- Re-recorded the stdio demo against the v0.40.0 tool surface.

## [0.40.0] - 2026-06-11

### Fixed

- **NCBI rate limit no longer over-claims on `NCBI_EMAIL` alone.** The limiter granted
  10 req/s when either `NCBI_API_KEY` or `NCBI_EMAIL` was set; NCBI only raises the
  ceiling for an API key (email is identification). Email-only configs now correctly
  stay at 3 req/s instead of inviting 429s across omics/literature/taxonomy.
- **Search fan-out survives sub-task cancellation.** Both fan-out paths checked
  `isinstance(outcome, Exception)`, so an `asyncio.CancelledError` (a `BaseException`)
  fell through to an assert and destroyed all sources' results. Both now guard on
  `BaseException`, matching `relate`'s handling — a cancelled adapter degrades to a
  per-source error with partial results returned.
- **Cursor decode validates field types.** A crafted cursor (e.g. `variants` as a
  string) previously fanned out garbage one-character queries; `decode` now rejects
  non-list `variants`, non-dict `offsets`, and non-positive `size` as corrupt.
- **`collapse_mirrors` finds transitive merges.** Greedy single-pass grouping could
  strand a byte-identical copy depending on arrival order (A↔C via md5, B↔C via
  sha — B stranded); groups sharing any checksum or fingerprint key are now unioned
  to a fixpoint. Survivor selection unchanged.
- **DataONE Solr queries escape user input.** A query could restructure the boolean
  and strip the `formatType:METADATA` filter; pids with embedded quotes broke the
  phrase query. Lucene specials are now escaped in `search`, and `resolve` escapes
  pid/resourceMap values.
- **GWAS pagination uses the capped page size.** `size>50` computed the page number
  from the raw size while requesting capped pages, skipping result windows.
- **OSF file listing is page-capped** (10 pages ≈ 500 files), matching the DANDI/
  CELLxGENE manifest-cap pattern, instead of following `links.next` unboundedly.
- **DataCite/Scholix error-taxonomy escapes closed.** A 200 without `data` raised a
  bare `KeyError` from DataCite resolve (now a typed `NotFoundError`); a non-JSON
  Scholix body raised `JSONDecodeError` through the resolve path (links are
  enrichment — now degrades to no links).
- **EuropePMC lookups phrase-quote PMCID/DOI values** (second-order injection guard
  for identifier values arriving from upstream APIs).
- **OpenNeuro snapshot queries use GraphQL variables** instead of f-string
  interpolation into the query text.

### Security

- **Archive extraction streams in 64 KiB chunks** counting actual bytes (not declared
  header sizes) against the ceiling, unlinking partials mid-stream on overrun — and no
  longer loads whole members into RAM.
- **Extraction shares the fetch byte budget.** `extract=true` previously granted the
  full `max_bytes` again after the download consumed it (up to 2× disk write); the
  archive now extracts into the remaining headroom and debits what it writes.
- **URL scheme allowlist on fetch and operate.** `FileEntry.url` values from upstream
  metadata are rejected unless `http(s)`; `operate` additionally allows `file://` only
  with `DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS=1` (test fixtures), closing a poisoned-
  metadata local-file read via the fsspec schema/preview paths.
- **Registry publish workflow hardened**: `actions/checkout` SHA-pinned like every
  other workflow, and the `mcp-publisher` binary install is sha256-verified instead
  of `curl | tar`.

### Changed

- **Tool descriptions now tell the whole truth.** `search`'s `sources` list names all
  12 adapters (dandi/openml/pdb/gwas/cellxgene were hidden); `fetch`'s fetchable list
  covers every wired backend with its verification status; `list_sources` health
  probing is scoped to the 5 actually-probed sources; `multi_query`'s always-on
  semantic re-rank is documented; the GWAS catalog entry states its exact
  disease-trait matching.
- **`server.json` documents the full env surface** (`LLM_API_BASE`/`LLM_API_KEY`/
  `LLM_MODEL`, `EMBEDDING_API_BASE`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL`,
  `UNPAYWALL_EMAIL` — previously only `NCBI_API_KEY`), so registry/deployment tooling
  can surface the knobs behind `understand=`, `multi_query=`, `rank=semantic`, and the
  Unpaywall full-text leg.
- README tool signatures updated to the live parameter surface (search filters,
  resolve `trust`/`fair`/`use`, operate `peek`); `PUBLISH.md` made version-agnostic
  (was frozen at the v0.11.0 instructions); the packaging test asserts version sync
  without a hardcoded literal.
- README visual refresh: the intro and sources table cover the full 12-source
  roster (DANDI, CELLxGENE, OpenML, RCSB PDB, GWAS Catalog, and
  DataCite→OpenNeuro were missing), a new architecture diagram
  (`docs/assets/architecture.svg`), and absolute asset/link URLs so the PyPI
  long description renders the demo and links correctly.

## [0.39.1] - 2026-06-11

### Fixed

- **`relate` now canonicalizes DOI forms (L1).** `relate` matched ids by exact string,
  so the same DOI in bare (`10.x`), `doi:`-scheme, and resolver-URL
  (`https://doi.org/10.x`) form failed to match — `version_lineage`, `explicit_link`,
  and `shared_identifier` silently missed real connections (e.g. a DataCite version
  edge expressed as a resolver URL). The identifier normalizer now strips DOI
  resolver/scheme prefixes, symmetrically across all four detectors.
- **`relate` reports version cycles as contradictions (L2).** A mutual / cyclic
  `superseded_by` (contradictory upstream metadata) previously yielded a single hint
  whose newer/older direction was arbitrary. `version_lineage` now reachability-detects
  cycles of any length and emits a contradiction hint (no asserted direction) for
  cyclic pairs, the normal directional hint otherwise.

### Changed

- Refreshed `examples/assets/demo.svg` to show all six tools (adds `operate`, `relate`)
  and the current source roster.

## [0.39.0] - 2026-06-11

### Added

- **`relate(ids)` — cross-resource join/harmonization hints (B9).** A 6th tool: given
  2–10 resource ids, it resolves each (cached) and emits evidence-backed, metadata-level
  hints — shared accession (BioProject/SRA/GEO), shared cross-identifier (doi/pmid/pmcid),
  explicit link between inputs, and version lineage. HINTS ONLY: no file reads, no column
  comparison, no executed joins. Per-id resolve failures are reported, not fatal.

## [0.38.0] - 2026-06-11

### Fixed

- **`search(understand=true)` no longer regresses recall.** A live recall eval against a
  verified gold set measured a mean recall@20 lift of **−0.40** for `understand=true`: the
  rewriter promoted _every_ LLM-inferred facet (organism/disease/tissue/chemical/assay) into a
  mandatory ANDed clause via the `_expand_*` resolvers, plus a `kind` post-filter — so a
  multi-facet natural-language query collapsed its result set (e.g. 20 → 2) against free-text
  keyword upstreams whose metadata can't satisfy every facet. The cross-domain efficacy of
  query-understanding never transferred to a stateless keyword fan-out.
  - **Fix (mechanism removal):** `understand=true` now _normalizes_ the query rather than
    synthesizing a faceted one. `keyword_core` retains the scientific/entity terms (only
    conversational fluff is stripped), and the entity facets **and `kind`** are **echoed in
    `query_understanding.extracted` for transparency but never auto-applied**. Only
    caller-passed facets drive the `_expand_*` resolvers / `kind` filter; explicit `year`
    scopes are still applied.
  - **Result:** mean recall@20 lift improved from −0.40 to −0.10 (4/5 verified queries now
    neutral-or-positive). The remaining variance is per-query keyword-rewrite term-loss
    (inherent to NL→keyword rewriting), not a structural bug. `understand=true` remains
    opt-in / default-off and is best validated per-deployment with your own LLM;
    `multi_query=true` (recall can only go up) is the safer recall lever.

### Changed

- **Verified recall-eval gold sets.** The `scripts/eval_understand_fixture.json` and
  `scripts/eval_multi_query_fixture.json` anchor sets were rebuilt — the previous fixtures
  paired real-looking DOIs with unrelated or no-longer-resolving records. Each anchor is now
  verified live (resolves _and_ on-topic by title). The eval harness code was unchanged.

## [0.37.0] - 2026-06-10

### Added

- **`search(multi_query=true)` — opt-in diverse multi-query recall expansion (A2.P2).** When
  enabled AND an LLM endpoint is configured, the LLM generates up to a few DELIBERATELY-DIVERSE
  reformulations of the query (different facets/synonyms/framings, not paraphrases), each is
  fanned out across every source, and the deduped union is re-ranked against your ORIGINAL
  query — surfacing relevant records a single keyword query would miss. This is phase 2 of A2
  (the biggest query-side recall lever); it composes with `understand=` (P1 structures one
  query, P2 fans out N variants). P3 (OpenAlex semantic federation) is optional next.
  - **The original query is ALWAYS variant 0.** Recall can only go UP — multi-query adds
    candidates on top of the (post-`understand`, post-ontology-expansion) single-query
    baseline; it never drops below it. Variants are case-insensitively deduped and capped at
    `MAX_QUERY_VARIANTS` (4, incl. the original), bounding the N× upstream cost.
  - **Composite-key window-paginated fan-out.** The single-query offset/cursor model keys by
    `source` and is left BYTE-IDENTICAL; multi-query runs a PARALLEL fan-out keyed by a
    composite `(variant_index, source)` label. Cross-variant duplicates (the same record
    surfaced by two variants) dedup to ONE — recall without duplication. Pagination advances
    per composite key; the cursor stores the EXPANDED variant strings so a continuation
    re-fans the frozen variants with NO LLM call and NO re-expansion.
  - **Re-rank anchored on the ORIGINAL query.** The union has no single coherent upstream
    order, so multi-query always engages the window-rank consumption model and re-ranks the
    whole window against the user's original pre-expansion query via the shipped
    `embeddings.rerank`. No embedding endpoint → interleaved order + an `errors['semantic']`
    note (still a recall win, just unranked — honest).
  - **Opt-in, fail-soft, zero new required deps.** A new `query_understanding.expand` mirrors
    the existing `rewrite`/`embeddings` fail-soft discipline exactly: enabled only by
    `LLM_API_BASE`. With no endpoint configured — or on ANY LLM/parse failure — the search
    degrades to a normal single-query search (variant 0) with a transparency note in
    `errors['multi_query']`; the LLM call can NEVER raise into the search path.
  - **Transparent echo.** A new `SearchResult.query_expansion` echoes the original `input` and
    the raw `variants` actually fanned out (original first); per-variant ontology expansion is
    still shown by the `*_expansion` echoes (the same params apply to every variant).
  - **Byte-identical single-query path.** With the flag off (the default) search is
    byte-identical and no LLM call is attempted; the entire pre-existing search + cursor suite
    passes untouched. Embedding-distance variant diversity is deferred (v1 uses
    prompt-demanded diversity + case-insensitive dedup).
  - **Eval harness shipped.** A gated (`DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE`)
    `scripts/eval_multi_query.py` + labeled JSON fixture runs each query with multi-query off
    vs on and prints per-query + mean recall@20 lift.

## [0.36.0] - 2026-06-10

### Added

- **`search(understand=true)` — opt-in LLM NL→structured-query rewriting (A2.P1).** When
  enabled AND an LLM endpoint is configured, a free-text query is rewritten into a keyword
  core + structured params (organism/disease/tissue/chemical/assay + kind/year) BEFORE the
  existing fan-out runs — raising recall on the QUERY side without an owned corpus or vector
  index. Off by default; this is phase 1 of A2 (P2 diverse multi-query expansion is next).
  - **Propose-validate-dispose guardrail — the LLM proposes, the deterministic resolvers
    dispose.** The rewriter only PROPOSES entities (`organism="Zea mays"`, etc.); the
    already-shipped `_expand_organism`/`_disease`/`_tissue`/`_chemical`/`_assay` resolvers
    (NCBI Taxonomy / MeSH / UBERON / ChEBI / EDAM) and the `kind`/year validators are the
    sole trust surface. A hallucinated entity that doesn't resolve simply yields no
    expansion — exactly like a user typo. No new fabricated taxonomy, no new trust surface.
  - **Opt-in, fail-soft, zero new required deps.** A new `llm.py` mirrors the existing
    `embeddings.py` pattern exactly: an OpenAI-compatible `/chat/completions` endpoint
    enabled only by `LLM_API_BASE` (`LLM_API_KEY` optional for keyless local servers,
    `LLM_MODEL` a passthrough string defaulting to `gpt-4o-mini`). With no endpoint
    configured — or on ANY LLM/parse error — the search runs byte-identically to before (the
    raw keyword query) with a transparency note in `errors['understand']`; the LLM call can
    NEVER raise into the search path.
  - **Explicit caller params always win.** The rewriter only FILLS fields the caller left
    None; if the caller passed `organism=`/`kind=`/a year explicitly, the LLM's value for
    that field is ignored and recorded under `overridden`.
  - **Transparent echo.** A new `SearchResult.query_understanding` echoes the raw `input`,
    the `keyword_core` actually used, the full `extracted` interpretation (every non-null
    field the LLM returned), the `applied` subset, and the `overridden` fields — mirroring
    the `*_expansion` echo honesty. Nothing the LLM did to the query is hidden; ontology
    entities still had to resolve to expand, which the `*_expansion` echoes show.
  - **Pagination stays consistent.** Understanding runs ONCE on the fresh search and mutates
    query/params BEFORE the cursor is encoded, so a paged understood-search replays the
    POST-rewrite query/params page-to-page; the continuation branch never re-understands.
  - **Eval harness shipped.** A gated (`DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE`)
    `scripts/eval_understand.py` + labeled JSON fixture runs each NL query with understand
    off vs on and prints per-query + mean recall@20 lift (a "show it works" instrument, not
    a hard assertion — live recall varies).
  - **Additive.** With the flag off (the default) search is byte-identical and no LLM call is
    attempted; the entire pre-existing search suite passes untouched.

## [0.35.0] - 2026-06-10

### Added

- **`operate(op="peek")` — a pre-download, normalized column profile.** A new mode on the
  existing `operate` tool that profiles every column of a remote tabular file WITHOUT
  downloading it: per-column type, null-rate, approximate distinct count, min/max, and
  numeric quartiles. This turns `operate` into a discovery-time advantage — a gateway that
  only proxies bytes can't answer "what does this file actually contain?" before a fetch.
  - **One DuckDB `SUMMARIZE`, reusing the hardened engine.** `peek` routes through the same
    `duckquery._connect` lockdown path as head/sql — the source is materialized into the
    in-memory `data` table while the local FS is still enabled, then the FS is sealed
    (`disabled_filesystems='LocalFileSystem'`) and config locked, and `SUMMARIZE data` runs
    against the in-memory table AFTER the lock. `peek` takes NO user SQL, so it adds no
    injection surface; the lockdown sequence is untouched.
  - **Honest naming.** The approximate distinct count is surfaced as `approx_unique` (a
    HyperLogLog estimate — never a bare `distinct`/`unique` implying exactness);
    `null_percentage` is a real computed `float` (e.g. one null in three → `33.33`);
    numeric stats (`avg`/`std`/`q25`/`q50`/`q75`) are `None` for text columns rather than a
    fabricated `0`. The per-column SUMMARIZE `count` is OMITTED: it is the TOTAL row count
    (identical for every column), NOT a non-null count, so surfacing it as "count" would
    mislead — the top-level `row_count` plus `null_percentage` give non-null counts honestly.
  - **Normalized across formats.** Parquet and CSV yield the same profile keys, so a caller
    gets one uniform answer regardless of source format.
  - **Size-gated like head/sql.** `SUMMARIZE` scans the whole materialized table, so `peek`
    has the same RAM profile as head/sql and honors `SOURCE_BYTE_CEILING` (100 MB) — an
    oversized source fails loud instead of OOMing. `schema`/`preview` stay ungated.
  - **Additive.** The four existing ops (`schema`/`preview`/`head`/`sql`) are byte-identical;
    `peek` is purely new (one enum value + one engine function).
  - **Deferred (noted for the future):** a Parquet-footer fast path could read null_count/
    min/max from the footer (range-reads only, skipping the full load and the size gate for
    Parquet), but it splits Parquet vs CSV into two fidelity paths and breaks the "one
    normalized answer" property — SUMMARIZE-for-both is the honest, uniform v1.

## [0.34.0] - 2026-06-10

### Added

- **`search(provenance=true)` — a whole-search RO-Crate 1.1 Run Crate.** An opt-in flag that
  attaches `provenance_crate{}` — a single machine-readable manifest documenting an ENTIRE search
  page in one call: the run itself (the query, the sources observed to participate, the ontology
  expansions that fired, and the per-source errors) PLUS per-hit provenance for every result. This
  is the "why an aggregator" artifact — the AI-Act training-data-manifest moment — and it completes
  the **B10 flagship** (B10a per-record dossier + B10b whole-search Run Crate).
  - **Run-level provenance as a `CreateAction`.** The new pure `run_crate.render(result)` emits a
    flat RO-Crate 1.1 `@graph` whose root `Dataset` (`name "Search run: <query>"`) `hasPart`s the
    hits and `mentions` a `#search-action` `CreateAction` (`instrument`→the `data-aggregator-mcp`
    `SoftwareApplication` agent carrying `__version__`, `object`→`./`). The action encodes the
    `query`, `result_count`/`total`, `sources_queried` (the union of sources that returned a hit and
    sources that errored — honestly NOT the full configured adapter set, which the result does not
    recover), the `ontology_expansions` that fired (one object per `taxon`/`mesh`/`tissue`/
    `chemical`/`assay` axis, naming the input, ontology id, canonical name, and synonyms added), and
    the per-source `errors` verbatim — a partial search is DISCLOSED, not hidden.
  - **Per-hit signals reuse the B10a dossier helpers.** Each `#hit-i` `Dataset` carries
    version-currency (B1), licence + normalized SPDX (B3), and FAIR (B4) assessment entities,
    composed by REUSING `dossier.assessment_entities(hit, id_prefix=f"hit-{i}-")` — the dossier's
    five per-signal helpers gained an `id_prefix` parameter (default `""`, so B10a output stays
    byte-identical) and a public `assessment_entities` reuse seam. FAIR is computed per hit via the
    pure `fair.assess`, so `render` stays PURE — no network/file I/O.
  - **No per-hit retraction, no per-hit Crossref.** Search hits carry `trust=None`, so the retraction
    helper emits NOTHING (an honest absence, never a "not retracted" claim). The run crate does NOT
    fan out N Crossref calls; per-hit retraction stays a per-record opt-in via
    `resolve(format=provenance)` (B10a).
  - **Intra-page boundary.** The crate documents the search page just returned; a paginated search
    yields one crate per page (stateless, mirroring B7). A cross-page run crate is out of scope.
  - **Opt-in + additive.** Default off; with the flag off, `search` output is byte-identical to
    pre-B10b. `conformsTo` stays ONLY on the metadata descriptor (`https://w3id.org/ro/crate/1.1`) —
    no fabricated profile URI.

## [0.33.0] - 2026-06-10

### Added

- **`resolve(format=provenance)` — a one-call RO-Crate 1.1 data-availability dossier.** An opt-in
  export that renders a single machine-readable artifact COMPOSING every provenance/integrity
  signal the server already computes for one resolved record: version-currency (B1
  `is_latest`/`superseded_by`), licence + normalized SPDX (B3), FAIRness (B4 `fair`), retraction /
  expression-of-concern (`trust`), and the source/DOI/ID chain (`source`, canonical id, `doi`,
  cross-`identifiers`, `accessions`, qualified `links` rel→target). The dossier is attached under a
  new `DataResource.provenance` field. This is the "why an aggregator" artifact for the
  data-provenance moment — we are the single point that holds all of these signals at once.
  - **Plain RO-Crate 1.1, not a Run Crate.** The new pure `dossier.render(resource)` REUSES
    `ro_crate.render` as the base graph (metadata descriptor + root `Dataset` + file entities) so the
    base crate can't drift, then EXTENDS `@graph` with a schema.org `CreateAction`
    (`#provenance-assessment`, `instrument`→the `data-aggregator-mcp` `SoftwareApplication` agent
    carrying `__version__`, `object`→`./`, `result`→the assessment entities) plus one `PropertyValue`
    per PRESENT signal. `conformsTo` stays ONLY on the metadata descriptor
    (`https://w3id.org/ro/crate/1.1`) — no fabricated profile URI. (The "Provenance Run Crate" term
    is the Workflow-Run-Crate profile for workflow executions — the wrong shape for a per-record
    data-availability dossier.)
  - **Unknown is NEVER a negative claim (the honesty contract).** Only signals actually present are
    represented. An unknown retraction (`trust.retracted is None`) is reported as
    "unknown / not checked", NEVER "not retracted"; a definitive `False` may state "no retraction on
    record (Crossref)". An unrecognized licence is "unrecognized", never an invented SPDX. A missing
    version/FAIR/trust signal is OMITTED, not fabricated. `render` is PURE/deterministic — no
    network or file I/O.
  - **One-call completeness.** `format=provenance` AUTO-attaches `fair` (pure local assess) and
    `trust` (one Crossref call) before rendering, so the dossier is whole in one call — REUSING any
    enricher already attached via `fair=true`/`trust=true` (idempotent, no double-compute). It does
    NOT set the `croissant`/`ro_crate` fields; those stay opt-in via their own format values.
  - **Follow-up:** the whole-search **Run Crate** (one call documenting every source queried +
    per-hit provenance for an entire result set) is the chosen next wave, **B10b**. B10a stays
    per-record so each wave is tight and reviewable.

## [0.32.0] - 2026-06-10

### Added

- **`search(chemical=…)` + `search(assay=…)` — recall axes 4 & 5: ChEBI compounds and EDAM
  assays/methods.** Two more ontology-grounded search-input expansion axes, cloning the proven
  `organism=`/`disease=`/`tissue=` shape: a resolved term ANDs its canonical name + exact
  synonyms into the query as an OR-group, so a single keyword recalls every surface form a
  single-source tool would miss. `chemical=caffeine` ANDs in `1,3,7-trimethylxanthine` (…);
  `assay=ChIP-seq` ANDs in `ChIP-sequencing`/`ChIP-exo` (…). Both are echoed for transparency in
  the new `SearchResult.chemical_expansion` / `assay_expansion` fields and round-trip through the
  pagination cursor (a continuation page does **not** re-expand — the echoes are frozen).
  - **ChEBI backs `chemical=`** (EBI OLS `ontology=chebi`). As with UBERON, two client-side
    filters are load-bearing because the OLS params do not self-enforce: `obo_id` must start with
    `CHEBI:` (cross-ontology leak guard), and an **exact** case-insensitive match of the input to
    the label OR a synonym is required (`exact=true` does not hard-filter — `q=aspirin` surfaces
    "aspirin-triggered protectin D1" before the real `aspirin`/`CHEBI:15365`). No exact match →
    no expansion (conservative; never guess a term).
  - **ChEBI synonyms are capped** to `_MAX_SYNONYMS = 12` (canonical always retained). ChEBI
    synonym lists are large (many IUPAC variants); the cap keeps the ANDed OR-group a sane query
    size. UBERON/MeSH need no cap.
  - **EDAM backs `assay=`** (EBI OLS `ontology=edam`, NOT OBI — OBI returns the term with an
    empty `synonym`, i.e. zero recall value). EDAM mixes id-classes (`topic_`/`data_`/`format_`/
    `operation_`); the filter restricts to **`obo_id.startswith("EDAM:topic_")`** — assay/method
    concepts are EDAM _topics_, so `data_`/`format_`/`operation_` ids are rejected.
  - **Fail-LOUD on an OLS error**, in parity with the organism/disease/tissue axes: a ChEBI
    lookup failure is recorded under `errors["chebi"]`, an EDAM failure under `errors["edam"]`,
    and the query runs **un-expanded** (these are search-input expansions, the opposite of a
    fail-soft resolve enricher — a silently-dropped expansion would make the model conclude the
    synonyms found nothing). A _no-match_ is not an error: the query is returned un-expanded with
    nothing recorded. The pure `_pick_*` matchers are deterministic; HTTP failures propagate and
    are NOT cached.

## [0.31.0] - 2026-06-10

### Added

- **`search(collapse_mirrors=true)` — opt-in cross-repo content dedup beyond DOI.** On top of
  the always-on exact-DOI dedup (`router._dedup`), this folds records that are the **same
  dataset deposited under different (or no) DOIs** — e.g. a Zenodo mirror of a figshare
  deposit, GEO↔ArrayExpress — into **one** record, annotating the survivor with the folded
  copies under a new **`mirrors[]`** field (`Mirror{source, id, doi}`). This is the
  aggregator-native moat extension: only possible because we fan out across 12 sources, so a
  cross-repo mirror is structurally invisible to any single-source tool.
  - **Conservative by design (the load-bearing safety decision).** A false merge silently
    hides a genuinely distinct dataset — worse than a missed merge — so collapse is **opt-in**
    (default `false`; DOI dedup unchanged) and the fingerprint is **high-confidence only**.
    Two records merge **iff** they share ANY full `algo:hex` `files[].checksum` (byte-identical
    → definitional identity) **OR** have an identical fingerprint key =
    `(normalized-title, first-author-surname, year)` with **all three present and non-empty**.
    `_normalize_title` lowercases, drops punctuation and collapses whitespace, then compares
    for **exact** normalized equality (never substring, never fuzzy). A title-only match, a
    missing year, or absent creators on either side does **not** merge. When in doubt, no merge.
    - **The fingerprint path requires DIFFERENT sources** (the checksum path stays
      source-agnostic). B7 is _cross-repo_ dedup: two SAME-source records sharing
      title+author+year are almost always **version siblings** (e.g. Zenodo record v1/v2),
      already modeled by version-currency (`is_latest`/`superseded_by`) — folding them as
      mirrors would be wrong. Only a copy in a different repository is a mirror. (Borne out on
      live data: the dominant real fingerprint collision is consecutive same-source Zenodo
      deposits, correctly left unfolded.)
  - **Annotate, never silently drop.** Every folded copy appears in the survivor's `mirrors[]`
    with its `source`+`id`(+`doi`); no record is ever its own mirror. Survivor selection is
    deterministic: DOI-bearing beats DOI-less; among DOI-bearing, a native id beats a
    `datacite:`-prefixed one (same precedence spirit as `_dedup`); first-seen order breaks ties.
  - **Pagination untouched.** `_collapse_mirrors` is a **pure** presentation-layer fold over the
    already-emitted page, run **after** the `consumed`/offset/`next_cursor` accounting, so it
    cannot corrupt offsets or stall pagination — a folded mirror merely makes a page return
    fewer than `size` items. The flag round-trips through the pagination cursor so continuation
    pages keep collapsing.
  - **Honest scope = intra-page, best-effort.** A mirror that lands on a different page (or past
    the `size` cut) is not collapsed — the stateless server holds no cross-page index.
  - **Deferred follow-ups (noted, out of scope):** (1) fuzzy / shingle / near-duplicate title
    matching (false-merge risk; v1 stays exact-normalized-title + author + year, or shared
    checksum); (2) cross-page mirror collapse (needs state the stateless server doesn't hold);
    (3) default-on collapse (stays opt-in until the fingerprint proves low-false-merge in the field).

## [0.30.0] - 2026-06-10

### Added

- **`resolve(use=<intent>)` — licence-compatibility preflight.** A new opt-in enricher
  attaches a `license_compat{}` advisory to a resolved record: an **ALLOW / REVIEW / DENY**
  verdict for an intended use, naming the governing licence clause and the normalized SPDX
  id. Supported intents are `commercial`, `redistribute`, `modify`, and `ml-training`
  (training is treated as a derivative + commercial use — `commercial-use` + `modifications`
  — which is **our stated interpretation**, documented in the module). The verdict is a
  **pure, local function** (no network call, unlike `trust`) over a **bundled licence
  matrix** whose permission/condition/limitation flags are drawn verbatim from the
  [choosealicense.com](https://github.com/github/choosealicense.com) flag vocabulary
  (vendored into Licensee → GitHub's Licenses API), fetched 2026-06-10, keyed on SPDX id.
  `normalize_spdx` maps bare SPDX ids, spaced/cased prose ("Apache License 2.0", "CC BY 4.0")
  and Creative-Commons / Open-Data-Commons URLs to a canonical SPDX id. **Honest coverage:**
  an unrecognized or absent licence yields `REVIEW` with `spdx_id=null` (defaults to
  all-rights-reserved) — never a fabricated ALLOW/DENY; a `DENY` always names the missing
  permission and its human clause (e.g. "commercial-use not granted (NonCommercial)"); a
  copyleft `same-license`/`disclose-source` obligation downgrades an otherwise-ALLOW
  `redistribute`/`ml-training` to `REVIEW`. An unknown intent fails **loud** (`ValueError`).
  Every verdict carries a **not-legal-advice** disclaimer: it is a metadata-derived
  compatibility _advisory_, not a legal determination. Resolve-only for v1 (parity with
  `trust`/`fair`).
  - **Deferred follow-ups (noted, out of scope):** (1) Croissant `usageInfo` → full
    `odrl:Offer` upgrade — the `LICENSE_MATRIX` built here is the reusable backend, but
    emitting structured ODRL triples would reverse B2's `test_no_odrl_permission_keys_in_b2_output`
    pin and belongs with the B10 dossier; (2) a search-time `use=` filter / advisory column
    across a whole result set (a `strict_license` enforced-fetch gate is B12).

## [0.29.0] - 2026-06-10

### Added

- **`resolve(fair=true)` — RDA-grounded FAIRness score.** A new opt-in enricher attaches
  a `fair{}` assessment to a resolved record: a 0–100 overall score plus
  `findable`/`accessible`/`interoperable`/`reusable` sub-scores, an `assessed` count, and
  a list of actionable `gaps`. Scoring is a **pure, local function** over the normalized
  `DataResource` (no network call), grounded in the machine-evaluable subset of the
  [RDA FAIR Data Maturity Model](https://doi.org/10.15497/rda00050) (Specification &
  Guidelines v0.90). Each gap names its RDA indicator id (e.g. `RDA-R1.1-03M`) and is
  framed as a metadata-exposure gap, never a value judgement about the dataset. Only
  indicators evaluable from the metadata we hold are scored — `assessed` reports exactly
  how many, so the score never fabricates a pass/fail for what the metadata cannot show.
  Licence presence (`RDA-R1.1-01M`) and machine-readability (`RDA-R1.1-03M`) are scored
  as **distinct** indicators: a free-text licence ("see LICENSE.txt") passes the former
  and fails the latter, while an SPDX/CC id ("cc-by-4.0", "MIT") passes both. Score math:
  per-dimension = `round(100 * passed-weight / total-weight)` with priority weights
  Essential=3 / Important=2 / Useful=1; overall = `round(mean of the 4 dimensions)`.
  Resolve-only for v1 (parity with `trust`); search-time FAIR is a deferred follow-up.

## [0.28.0] - 2026-06-10

### Changed

- **Croissant export upgraded to Croissant 1.1** — the `format=croissant` manifest now
  carries the `conformsTo` version marker (`http://mlcommons.org/croissant/1.1`) and a
  `@context` that declares the `dct`/`prov`/`odrl` namespace prefixes. The export gains
  dataset-level **PROV-O provenance** populated from our cross-source enrichment:
  `prov:wasAttributedTo` (creators, with an ORCID `@id` when known) and
  `prov:wasDerivedFrom` mapped **conservatively** from only true-derivation link rels
  (`is_derived_from`/`is_version_of`/`is_new_version_of`) — `is_supplement_to`/`cites`/
  `part_of` are deliberately NOT mapped, so the manifest never overstates provenance.
  Also now emits the schema.org fields we already hold: `keywords` (subjects),
  `dateModified` (last-updated), `publisher` (source display-name), and `citeAs` (only
  when the record was resolved with a bibtex citation). A minimal honest `usageInfo`
  license pointer is added when a license is present; the full ODRL `odrl:Offer` policy
  is deferred to B3 (license-compatibility) — B2 asserts no permissions. The renderer
  stays a pure transform (no I/O). Still file-level: no RecordSet/Field structures.

## [0.27.0] - 2026-06-10

### Added

- **UBERON tissue query-expansion** — `search(tissue=<name>)` resolves a tissue/anatomy
  name to its canonical UBERON term via the EBI OLS4 search API (`ontology=uberon`,
  `exact=true`) and expands the query with the canonical label plus its exact synonyms
  (e.g. `liver` also matches `iecur`/`jecur`). The third ontology-grounded recall axis
  after `organism=` (NCBI Taxonomy) and `disease=` (MeSH), and the first backed by a
  non-NCBI client. Especially additive for the single-cell sources (CELLxGENE/DANDI).
  Two client-side filters are load-bearing (neither OLS param self-enforces): the result
  must be a real `UBERON:` term (a bare relevance search leaks cross-ontology `PR:` hits)
  and the input must match the canonical label or an exact synonym (no expansion into a
  wrong term — a no-match yields no expansion). The expansion is echoed in
  `tissue_expansion`, composes with `organism=` and `disease=` (three AND-groups stack),
  and is fail-loud (a UBERON lookup failure surfaces in `errors["uberon"]` and the query
  runs un-expanded).

## [0.26.0] - 2026-06-10

### Added

- **MeSH disease query-expansion** — `search(disease=<name>)` resolves a disease/phenotype
  name to its canonical MeSH descriptor (NCBI E-utilities, `db=mesh`) and expands the query
  with the canonical descriptor name plus entry-term synonyms (e.g. `breast cancer` also
  matches `Breast Neoplasms`). True added recall the keyword window can't reach, grounded in
  a real ontology — the same shape as `organism=` taxonomy expansion. The expansion is echoed
  in `mesh_expansion`, composes with `organism=` (both AND-groups stack), and is fail-loud
  (a MeSH lookup failure surfaces in `errors["mesh"]` and the query runs un-expanded). The
  `[MeSH Terms]` field restriction collapses a lay name to its one canonical descriptor.

## [0.25.0] - 2026-06-10

### Added

- **MCP resources** — resolved records and the source catalog are now addressable as
  MCP resources (a separate primitive from tools). A client can read any record by URI
  `dataresource://record/{id}` (where `{id}` is the same source-prefixed id the `resolve`
  tool accepts, URL-encoded) and the source catalog at `dataresource://catalog`. Backed
  by the existing resolve pipeline; the `resources` capability is now advertised.

## [0.24.0] - 2026-06-10

### Added

- **Retraction trust signal** — `resolve(trust=true)` now attaches a `trust{}` block
  (`retracted` / `retraction_doi` / `concern`) derived from a single Crossref
  `/works/{doi}` lookup of the record's DOI. A DOI Crossref does not register (e.g. a
  DataCite data DOI) leaves `retracted=null` (unknown — never a false "clean" claim);
  a found-but-clean work is `retracted=false`. As an opt-in resolve enricher it is
  fail-soft (a Crossref outage degrades to unknown, never aborts the resolve). First of
  the Phase-4 trust signals (CoreTrustSeal / FAIR proxy deferred); reinforces the
  verified-fetch / anti-hallucination posture — callers can flag retracted records
  before handing them downstream.

## [0.23.0] - 2026-06-10

### Added

- **CZ CELLxGENE Discover** source — single-cell datasets via the Discover curation
  REST API. The collection is the resource unit (one publication DOI per collection);
  search filters client-side on each collection's tissue/disease/organism/assay
  ontology labels, and `resolve` attaches the H5AD/RDS download manifest (capped at
  200 files; direct URLs, unverified — the API exposes filesize but no checksum).
  `kind="dataset"`.

## [0.22.0] - 2026-06-10

### Added

- **DANDI Archive** source — neurophysiology dandisets (NWB) via the DANDI REST API:
  search + resolve, with a per-asset download manifest (capped at the first 100
  assets; 302→S3, unverified). DOI is attached from the published version's
  metadata (drafts have none). `kind="dataset"`.
- **OpenNeuro** fetch — OpenNeuro datasets (`10.18112/openneuro.*`) are now
  fetchable: discovery rides the existing DataCite firehose, and `resolve` attaches
  the snapshot's top-level file manifest via the OpenNeuro GraphQL API.

## [0.21.0] - 2026-06-10

### Added

- **RCSB PDB** source — macromolecular-structure discovery via the RCSB full-text
  search API, hydrated with title + primary-citation DOI/PubMed in one GraphQL
  batch call; `.cif`/`.pdb` structure files stream from files.rcsb.org (unverified —
  no upstream checksum). `kind="dataset"`.
- **GWAS Catalog** source — genome-wide association studies keyed by disease trait
  (EBI REST), carrying the PubMed cross-link for the paper↔data bridge.
  Discovery-only this wave (summary-statistics fetch deferred). `kind="study"`.
- **OpenML** source — machine-learning datasets via name-substring search; resolve
  attaches an md5-verified ARFF and the auto-converted Parquet, which is operable
  (schema/preview/head/sql via the `[operate]` extra). `kind="dataset"`.

### Changed

- **mypy is now a blocking CI gate.** Added mypy (dev dep + `[tool.mypy]` config) and a blocking `Types (mypy)` step to CI. Cleared the existing type debt with real `None`-handling fixes (narrowing asserts that match documented invariants + a walrus binding); no `# type: ignore` needed. No runtime change.

## [0.20.0] - 2026-06-02

### Added

- HuggingFace datasets are now operable via the datasets-server auto-converted
  Parquet: `huggingface.resolve()` surfaces those files (`source="hf-datasets-server"`),
  so `operate` (schema/preview/head/sql) reaches datasets stored as JSON/JSONL/arrow,
  not only ones that ship `.parquet` at the raw URL. Best-effort: a dataset with no
  converted view (gated/too-big/pending) keeps its raw siblings unchanged.

## [0.19.0] - 2026-06-01

### Added

- **`operate` tool (5th tool)** — inspect/query a remote tabular file (Parquet/CSV/TSV) without downloading it: `op="schema"` (columns+types), `"preview"` (sample), `"head"` (first n rows), `"sql"` (read-only SELECT against the file as the view `data`). Addresses a file by catalog id + file name. Requires the optional `[operate]` extra (`duckdb`/`pyarrow`/`fsspec`); the base install is unchanged.
- **`DataResource.access_modes`** — best-effort capability claim (`fetch` + operate modes), populated on `resolve`, degrading to `["fetch"]` when the `[operate]` extra is absent; `list_sources` flags `operable` sources.

### Security

- `operate(op="sql")` runs user SQL in a locked-down DuckDB: read-only, `disabled_filesystems='LocalFileSystem'` (httpfs only), `lock_configuration`, single-SELECT validation, plus row/byte/wall-clock caps.

## [0.18.0] - 2026-05-31

### Added

- **DataONE** source — eco/environmental federation (KNB, Arctic Data Center, PANGAEA, …) with verified fetch: data objects stream from Member Nodes with per-object MD5/SHA-256 checksum verification.
- **OmicsDI** source — proteomics/metabolomics discovery, restricted to the mass-spec modality repos (PRIDE, MassIVE, MetaboLights, Metabolomics Workbench, GNPS, PeptideAtlas) not already covered by the omics leg.
- **PRIDE** and **MetaboLights** fetch backends — `omicsdi:pride:*` / `omicsdi:metabolights_dataset:*` records fetch end-to-end over the EBI HTTPS mirror (unverified: no upstream checksum; PRIDE is size-checked). Other OmicsDI repos are discovery-only and fail loud at fetch with a source pointer.

### Fixed

- DataONE resolve follows the `/cn/v2/resolve/` **303** correctly: the Member-Node url is read from the `Location` header instead of chasing the redirect into the object bytes (which broke checksum verification). Live-validated end-to-end.
- MetaboLights file urls are sourced from the FTP directory listing, not the WS `/files` API, whose logical names don't always match the physical FTP filename (assay files 404'd).

### Notes

- OmicsDI contributes first-page results only (modality post-filtering precludes stable pagination).
- No dedup-ranking change: the existing binary rule already keeps the verified copy on every realistic DOI collision.

## [0.17.0] - 2026-05-31

### Added

- Structured-output round-trip gate (`tests/test_output_schema_gate.py`) — every
  tool's output is validated against its declared `outputSchema`, guarding against
  field drift between `model_dump()` and `model_json_schema()`.
- `DataResource.metrics` (citations/views/downloads/likes — separate axes, no
  blended score), populated from DataCite inline counts and HuggingFace
  downloads/likes.
- `DataResource.is_latest` / `superseded_by`, derived from version relations in
  `links[]` (fields only; no ranking change).
- `DataResource.last_updated` freshness (DataCite + HuggingFace).
- Tool annotations (`readOnlyHint` on search/resolve/list_sources; explicit
  read/destructive/idempotent hints on fetch).
- MCP prompts: `find_data`, `data_behind_paper`, `search_resolve_fetch`.
- Export: `resolve(format="croissant")` (file-level Croissant) and
  `resolve(format="ro-crate")` (RO-Crate 1.1).

## [0.16.0] - 2026-05-31

### Added

- Per-service rate limiting — an async token bucket paces outbound requests per
  upstream (NCBI 3/s, 10/s with `NCBI_API_KEY`/`NCBI_EMAIL`; generous elsewhere),
  acquired on every request and retry so a fan-out or 429-retry storm can't trip a
  documented limit.
- `list_sources(check_health=true)` — probes each source's base endpoint and
  attaches `{status, latency_ms, detail}` per source. The default call stays
  instant and network-free.
- `search(rank="semantic")` — re-ranks the fetched page by embedding similarity
  to the query via an optional OpenAI-compatible endpoint (`EMBEDDING_API_BASE`,
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`). Degrades to relevance order with an
  `errors["semantic"]` note when unconfigured or on failure. Semantic mode
  paginates window-by-window (each page consumes its full fetched window).

### Changed

- `resolve` results are cached in-process (TTL, default 3600s; `CACHE_TTL_SECONDS`
  to override, `0` disables). The previously unbounded taxonomy cache now uses the
  same bounded TTL+LRU cache.

## [0.15.0] - 2026-05-31

### Added

- HuggingFace datasets as a search/resolve/fetch source (`hf:<owner>/<name>`). Files
  are fetchable via the HF resolve URL (unverified — the API exposes no checksum/size).
  HF contributes to the first results page only (its API paginates by cursor, not offset).

## [0.14.0] - 2026-05-31

### Added

- `fetch` now downloads a resource's files in parallel (bounded concurrency).
- `fetch` resume — files already present and verified (by checksum, else size) are
  skipped and reported in `FetchResult.resumed`; a re-run is idempotent. `force=true`
  re-downloads everything.
- `fetch` emits MCP progress notifications as files complete when the caller supplies
  a `progressToken`.

## [0.13.0] - 2026-05-31

### Changed

- **Breaking:** `creators` is now a list of `{name, orcid}` objects (was a list of
  name strings). ORCID iDs are populated from DataCite `nameIdentifiers` and Zenodo
  creator metadata where available.

### Added

- `funding` — funding references (`{funder, award}`) from DataCite `fundingReferences`
  and Zenodo `grants`.
- Related-identifier `links` — DataCite `relatedIdentifiers` / Zenodo
  `related_identifiers` are surfaced as `links` (verbatim targets; no graph traversal).

## [0.12.0] - 2026-05-31

### Added

- `search` pagination — an opaque `next_cursor` walks past the first page of
  merged results; pass it back as `cursor` to fetch the next page (per-source
  offsets are packed into the token; `size` stays "deduped results per page").
- `search` filters — `published_after` / `published_before` (publication-year
  bounds) and `kind` constrain results. Filtering is applied to the fetched
  window on normalized fields; a record with no year is dropped when a year
  bound is set.
- `list_sources` now advertises `published_after` / `published_before` / `kind`
  / `cursor` in each source's `filters_supported`.

## [0.11.0] - 2026-05-29

### Added

- `fetch(extract=true)` — opt-in unpacking of downloaded zip/tar archives,
  guarded against path-traversal and runaway extracted size.
- `fetch` integrity check — an unverified `pdf`/`xml` download whose body is
  HTML (a login/paywall page) now fails loud instead of saving a bogus file.
- `resolve` of a Zenodo DOI via DataCite now populates `files[]` (delegates to
  the native Zenodo adapter); such ids are fetchable.
- BioProject `resolve` attaches `links[]` to its SRA runs.
- PubMed `resolve` populates the article abstract (`description`) and, for
  PMC open-access records, `access`/`license` (from EuropePMC/Unpaywall).
- `list_sources` reports per-source fetchability, id examples, and the
  `organism` filter.

### Fixed

- HTTP boundary now fully honors the fail-loud contract: transport-level errors
  (connect/read/timeout) and malformed HTTP-200 bodies (NCBI throttle envelopes)
  are retried and surface as a typed `DataAggregatorError`, on both the
  search/resolve path (`_http`) and the `fetch` streaming path.

## [0.10.0] - 2026-05-29

### Added

- Literature `resolve` (`pubmed:`/`openaire:`) attaches an open-access full-text
  file via an EuropePMC `fullTextXML` → Unpaywall `url_for_pdf` cascade (first
  hit wins; `FileEntry.source` labels the origin). Enrichment — fails soft.
- `DataResource.identifiers` — normalized `{pmid, pmcid, doi}` cross-identifiers.
  PubMed gets them free from esummary; OpenAIRE via the NCBI ID Converter.
- `FileEntry.source` — provenance label for an attached file.
- `pubmed:`/`openaire:` are now fetchable: `fetch` streams open-access full text
  (unverified — no upstream checksum, like GEO). Fails loud when a paper has no
  open full text.

### Changed

- New env var `UNPAYWALL_EMAIL` enables the Unpaywall fallback leg (the EuropePMC
  leg needs no key). `NCBI_EMAIL`/`UNPAYWALL_EMAIL` is forwarded to NCBI idconv.

### Notes

- Cascade deviates from the umbrella spec's literal "PMC → EuropePMC → Unpaywall":
  PMC's machine download is tgz-over-FTP (not HTTPS-fetchable), and EuropePMC
  already serves the PMC OA subset as HTTPS XML — so the dedicated PMC leg is
  dropped. Honors the spec's intent (open full text, first hit wins).
- No MeSH (ceded to the openalex MCP). Full text is open-access only; paywalled
  content is never bypassed.

## [0.9.0] - 2026-05-29

### Added

- `resolve(id, cite=<format>)` renders a citation onto the record — `bibtex`,
  `ris`, `csl-json`, or any CSL style name (`apa`, `mla`, `vancouver`, …). DOI
  records use DOI content negotiation (CrossRef + DataCite); non-DOI records
  produce CSL-JSON from metadata. Default off; failures degrade quietly.
- `DataResource.access` — normalized access status
  (`open`/`embargoed`/`restricted`/`closed`/`unknown`), populated from Zenodo
  `access_right`, OpenAIRE `bestAccessRight`, and an open-license signal on
  DataCite rights.
- `DataResource.citation` — holds the rendered citation when `cite=` is used.

### Changed

- OpenAIRE records now carry `license` (from the deposit instance) and `access`.

### Notes

- PMC license/access for `pubmed:` records is deferred to Phase 9 (bundled with
  PMC full-text retrieval). GEO/SRA/BioProject expose no rights → `access` stays
  null honestly.

## [0.8.0] - 2026-05-29

### Added

- DataCite-repo fetch: resolving a DataCite DOI now attaches `files[]` from the
  host repo's native API — **Figshare** (md5), **Dataverse** (Harvard default,
  `DATAVERSE_BASE_URL` override; md5), **OSF** (osfstorage, paginated; md5), all
  fetchable and checksum-verified. **Dryad** is manifest-only (names/sizes/
  sha-256) — its downloads are token/bot-challenge gated, so it is excluded from
  the fetch allowlist and fetching a Dryad DOI fails loud.
- New per-repo resolver modules: `figshare.py`, `dataverse.py`, `osf.py`, `dryad.py`.

### Changed

- The fetch allowlist accepts `datacite:` ids; fetchability is then decided
  post-resolve from the detected host repo (`_DATACITE_FETCHABLE`).

### Fixed

- DataCite source detection now recognizes Harvard Dataverse (client id
  `gdcc.harvard-dv`, which contains no "dataverse" substring).

## [0.7.0] - 2026-05-29

### Added

- Omics fetch: `fetch` now downloads SRA FASTQ files (via the ENA manifest,
  md5-verified) and GEO supplementary files (parsed from the GEO `suppl/`
  directory index; unverified — NCBI exposes no checksums there).
- New `geo.py` supplementary-file resolver; `geo:` resolves now populate
  `files[]`. A GEO record with no `suppl/` directory (HTTP 404) degrades to
  `files=[]` rather than failing.

### Changed

- The `fetch` tool resolves through `router.resolve` (source-agnostic) instead
  of a hardcoded Zenodo path; the `_FETCHABLE_SOURCES` allowlist now includes
  `sra:` and `geo:`.

## [0.6.0] - 2026-05-29

### Added

- Packaging for publication: `python -m data_aggregator_mcp` entry point,
  complete `[project.urls]` + `keywords`, Beta classifier.
- `server.json` for the official MCP registry
  (`io.github.musharna/data-aggregator-mcp`) + the `mcp-name:` ownership marker
  in the README.
- GitHub Actions: `ci.yml` (pytest + ruff, Python 3.11/3.12) and `publish.yml`
  (Release-triggered PyPI upload via OIDC trusted publishing — no stored token).
- `PUBLISH.md` runbook and user-facing install/use docs (`uvx`, `pip`,
  `claude mcp add`).

### Notes

- Prepare-to-the-gate: the public GitHub repo, the real PyPI upload, and the
  registry submission are documented manual steps, not executed here.
- HTTP transport remains deferred — distribution is local stdio via PyPI/`uvx`.

## [0.5.0] - 2026-05-28

### Added

- Unifying layer: NCBI-Taxonomy-backed **synonym expansion** on `search`. New
  optional `organism` param — resolves to a taxid and ANDs the query with the
  canonical name + synonyms (e.g. `Orobanche aegyptiaca` also matches
  `Phelipanche aegyptiaca`, taxid 99112). The expansion is echoed in
  `SearchResult.taxon_expansion`.
- **Organism normalization**: results/resolved records gain `taxa[]`
  (`{taxid, name}`) derived from raw `organism[]` via NCBI Taxonomy; raw strings
  are preserved.
- **Cross-links**: a `described_in` → `plant-genomics:taxid:<n>` link is attached
  for Viridiplantae (plant) taxa, the seam to the sibling `plant-genomics-mcp`.

### Notes

- No new search source and no new tool — Phase 5 is a taxonomy module plus a
  post-merge enrichment pass. `fetch` is unchanged (Zenodo-only).
- Enrichment incurs zero taxonomy calls for records without an organism. A
  taxonomy outage surfaces in `errors["taxonomy"]` on `search` (never silently
  dropped) and degrades gracefully on `resolve`.

## [0.4.0] - 2026-05-28

### Added

- Unified `literature` source: PubMed + OpenAIRE publication discovery, fanned
  out in parallel and merged. Registered as a fourth `search` source.
- Resolve-time paper→data links: resolving a `pubmed:` id attaches `links[]` to
  `sra:`/`geo:`/`bioproject:` ids via NCBI elink; resolving an `openaire:` id
  attaches `datacite:` links via the ScholeXplorer Scholix API. Publication↔
  publication citation edges are dropped — that is the standalone openalex MCP's
  job, not ours.

### Notes

- Literature is discovery-only — `fetch` stays Zenodo-only and fails loud for
  `pubmed:`/`openaire:` ids.
- OpenAIRE paper→dataset Scholix links are sparse (most paper edges are
  citations, which are dropped); the PubMed→GEO/SRA elink path is the reliable
  paper→data bridge. OpenAIRE's contribution is discovery breadth.

## [0.3.0] - 2026-05-28

### Added

- Unified NCBI omics source: GEO + SRA + BioProject discovery via E-utilities,
  fanned out internally and merged. Registered as a third `search` source.
- ENA filereport FASTQ manifest attached on `resolve` of an `sra:` id (direct
  https URLs).
- Optional `NCBI_API_KEY` env var to raise the NCBI rate limit (3→10 req/s).
- Shared round-robin `_merge.interleave` (extracted from the router) so the
  omics fan-out reuses fair merging.

### Notes

- Omics fetch is deferred — `fetch` remains Zenodo-only and fails loud for omics
  ids. GEO/BioProject are discovery-only (no file manifest in this phase).

## [0.2.0] - 2026-05-28

### Added

- DataCite discovery adapter — one query spans every DataCite client (Dryad,
  Figshare, Dataverse, OSF, Mendeley, …); metadata-only, so resources carry no
  file manifest.
- Multi-source router: `search` fans out across Zenodo + DataCite in parallel,
  round-robin merges results so the page limit never starves a later source,
  dedups by DOI (native fetch backends win over DataCite metadata), and surfaces
  per-source failures in `errors{}` instead of silently dropping a backend.
- `search` `sources` filter to restrict fan-out (e.g. `["datacite"]`).
- Shared `compact()` helper in `models` (extracted from the Zenodo adapter).

### Changed

- `resolve` routes by id shape (`zenodo:` / bare id / `datacite:` / bare DOI).
- `fetch` is Zenodo-only in Phase 2 and fails loud (`FetchNotSupportedError`)
  for discovery-only sources; per-repo fetch adapters come in a later phase.

## [0.1.0] - 2026-05-28

### Added

- Initial MCP server: search/resolve/fetch/list_sources over Zenodo.
- Normalized DataResource model; stream-to-disk fetch with max_bytes guard,
  checksum verification, and provenance sidecar.
