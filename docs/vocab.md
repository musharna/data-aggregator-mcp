# Crate vocabulary

The provenance dossier (`resolve(format=provenance)`) and the search run crate
(`search(provenance=true)`) are RO-Crate 1.1 crates. Most of what they say uses
schema.org terms from the RO-Crate context. The terms below have no schema.org
equivalent; each crate declares the ones it uses in its `@context` and defines them
in its graph as `rdf:Property` entities, as RO-Crate 1.1 asks of ad hoc terms.

Each term's IRI is `https://github.com/musharna/data-aggregator-mcp/blob/main/docs/vocab.md#<term>`, the heading below.

## is_latest

Whether the record is the latest known version of its work.

## superseded_by

Identifier of the newer version that supersedes the record.

## license_raw

The licence exactly as the source states it.

## normalized_spdx

The SPDX identifier the stated licence maps to, if any.

## score

FAIR score of the record, 0 to 100.

## findable

FAIR Findable sub-score, 0 to 100.

## accessible

FAIR Accessible sub-score, 0 to 100.

## interoperable

FAIR Interoperable sub-score, 0 to 100.

## reusable

FAIR Reusable sub-score, 0 to 100.

## assessed

Number of FAIR indicators evaluated.

## gaps

FAIR indicators the record does not meet.

## retracted

Whether a retraction is on record (Crossref); absent when unchecked.

## concern

Whether an expression of concern is on record (Crossref).

## retraction_doi

DOI of the retraction notice.

## source

The repository the record was resolved from.

## canonical_id

The record's source-prefixed identifier in this server.

## doi

The record's DOI, without a resolver prefix.

## identifiers

Other identifiers of the record, one PropertyValue each.

## accessions

Database accessions the record cites.

## links

Qualified relations of the record (relation name to target id).

## result_count

Number of hits on this search page.

## total

Total number of hits the sources report for the query.

## sources_queried

Sources observed to take part in this search page.

## ontology_expansions

Ontology terms the query was expanded with.

## axis

The search filter an ontology expansion applies to.

## matched_input

The text the ontology expansion was looked up from.
