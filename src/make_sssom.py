# ===================== Reviewed HPO -> UBERON / CL links  ->  SSSOM/TSV =====================
"""
make_sssom.py: publish the reviewed HPO -> UBERON / CL links as SSSOM mapping sets.

SSSOM (Simple Standard for Sharing Ontological Mappings) is the community
standard for exchanging mappings between ontology terms:
    https://mapping-commons.github.io/sssom/
This script writes one SSSOM/TSV file per target ontology:

    hp-uberon.sssom.tsv     HPO phenotypes -> Uberon anatomical entities
    hp-cl.sssom.tsv         HPO phenotypes -> Cell Ontology cell types

Where it sits in the pipeline
-----------------------------
    ontology_sources.py           choose / download HPO, UBERON and CL releases
    hpo_crosslink_extractor.py    direct links from HPO logical definitions
    hpo_ancestor_propagation.py   links for terms without one, from matching ancestors
    (review)                      every propagated row gets a Review_Verdict
    make_sssom.py                 THIS SCRIPT: reviewed tables -> SSSOM/TSV

Inputs
------
--reviewed-dir   folder with hpo_to_uberon_with_propagation_reviewed.csv and
                 hpo_to_cl_with_propagation_reviewed.csv (propagation output plus
                 the columns Review_Verdict, Review_Note, Suggested_Term_*)
--metadata       run_metadata.json of the propagation run that produced them;
                 it supplies the ontology releases and settings for the header
--creator-orcid  ORCID iD of the person publishing the set (creator_id)
--out            output folder (default ./data/outputs/sssom)

Which rows become mappings
--------------------------
* every DIRECT link: the UBERON/CL term appears in the HPO term's own logical
  definition (equivalentClass or anonymous subClassOf axiom)
* PROPAGATED links whose Review_Verdict is "ok"; rows judged too_generic,
  questionable, redundant or wrong are left out
* negated links (under owl:complementOf) are never included
* rows with the same subject, predicate, object and justification are merged
  into one mapping; their relation paths are joined in the comment

How each column is filled
-------------------------
subject_id / subject_label      the HPO term, e.g. HP:0000199 "Tongue nodules"
predicate_id / predicate_label  the RO/BFO relation directly before the target in
                                the HPO definition (see "Predicate" below)
object_id / object_label        the UBERON or CL term
mapping_justification           direct:     semapv:UnspecifiedMatching
                                propagated: semapv:CompositeMatching
                                (see "Justification" below)
subject_match_field             propagated only: rdfs:label (the HPO label is matched)
object_match_field              propagated only: what matched on the target side:
                                rdfs:label, oboInOwl:hasExactSynonym, or
                                UBPROP:0000007 (has_relational_adjective)
match_string                    propagated only: the text found in the HPO label
subject_preprocessing /         propagated only: semapv:CaseNormalization,
object_preprocessing            semapv:PunctuationElemination, semapv:Tokenization
                                (labels are lower-cased and split into words at
                                any non-alphanumeric character before matching)
comment                         how the link was found, including the full relation
                                path and, for propagated rows, the source ancestor

Set-level fields (header): mapping_set_id, version, title, description,
creator_id, license, subject_type / object_type (owl class), subject_source /
object_source (obo:hp.owl, obo:uberon.owl, obo:cl.owl) with their exact release
IRIs from run_metadata.json, mapping_provider (HPO), mapping_tool, mapping_date.

Predicate
---------
For  Tongue nodules = has part some (... characteristic of part of some tongue)
the mapping reads  HP:0000199  RO:0002314 (characteristic of part of)  UBERON:0001723.
This is a shorthand: in the definition, the relation connects a part of the
phenotype (a quality or structure), not the phenotype class itself, to the
anatomy. The full relation path is kept in the comment. The SSSOM model allows
any predicate and explicitly encourages Relation Ontology (RO) relations.

Justification (SEMAPV vocabulary)
---------------------------------
* Direct links are read from logical definitions written by HPO's curators.
  No matching algorithm or reasoner is involved on our side, so
  semapv:LogicalReasoning ("based on the inferences made by a logical
  reasoner") would be wrong. They use semapv:UnspecifiedMatching, and the set
  names HPO as mapping_provider, the SSSOM field for "an ontology that already
  contains the mappings".
* Propagated links combine two approaches, the HPO hierarchy (nearest ancestor
  with a link) and lexical matching (the target's name appears in the phenotype
  label), so they use semapv:CompositeMatching, documented with match fields,
  match string and preprocessing as the SSSOM justification guide recommends.
* The review that selected propagated rows was an automated plausibility check,
  not human curation, so no reviewer_id is written. Add reviewers only for rows
  a person has actually checked.

Conformance with the SSSOM/TSV specification
--------------------------------------------
https://mapping-commons.github.io/sssom/dev/spec-formats-tsv/ ; the output is
also canonical SSSOM/TSV (verified: SSSOM-Java 1.13.1 reads each file in
strict mode and writes it back byte-for-byte identical).
* identifiers are CURIEs; IRIs are contracted only through declared prefixes,
  and an IRI without a matching prefix stops the script
* curie_map: only prefixes actually used, no built-in prefixes, sorted
* UTF-8 without byte-order mark, LF line endings
* metadata: slots in the order of the MappingSet slot table, no space after
  '#', multi-valued slots as YAML block sequences, scalars in plain style where
  possible, otherwise double-quoted
* mappings: columns in the order of the Mapping slot table, no empty columns,
  rows sorted on all columns, values quoted only when needed, '|' and '\\'
  escaped inside multi-valued slots
* condensation: a propagatable slot with the same value in every row is moved
  to the metadata block
* only SSSOM 1.0 slots and values are used, so sssom_version is omitted

Validate a result with either reference tool:
    sssom validate hp-uberon.sssom.tsv                        (sssom-py)
    java -jar sssom-cli.jar hp-uberon.sssom.tsv -o check.tsv  (SSSOM-Java)

Usage
-----
python src/make_sssom.py \\
    --reviewed-dir data/outputs/ancestor_propagation/<run>/claude_reviews \\
    --metadata     data/outputs/ancestor_propagation/<run>/run_metadata.json \\
    --creator-orcid 0000-0002-1825-0097

Before sharing, replace the placeholder mapping_set_id (MAPPING_SET_BASE) with
the permanent URL where the files will live, and check the license.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd

# ============================== SETTINGS ===================================
OBO = "http://purl.obolibrary.org/obo/"

# Replace with the permanent location of the published files
MAPPING_SET_BASE = "https://example.org/REPLACE-ME/"
LICENSE = "https://creativecommons.org/licenses/by/4.0/"

PLACEHOLDER_ORCID = "0000-0000-0000-0000"   # used until --creator-orcid is given
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

# ============================== PREFIXES ===================================
# Built-in prefixes (SSSOM spec, "IRI prefixes"): usable without declaration;
# canonical writers must leave them out of the curie_map.
BUILTIN = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "semapv": "https://w3id.org/semapv/vocab/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sssom": "https://w3id.org/sssom/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "linkml": "https://w3id.org/linkml/",
}
# Prefixes this script may declare. Only those actually used appear in a file.
DECLARABLE = {
    "HP": OBO + "HP_", "UBERON": OBO + "UBERON_", "CL": OBO + "CL_",
    "RO": OBO + "RO_", "BFO": OBO + "BFO_", "BSPO": OBO + "BSPO_",
    "UBPROP": OBO + "UBPROP_", "obo": OBO,
    "oboInOwl": "http://www.geneontology.org/formats/oboInOwl#",
    "orcid": "https://orcid.org/",
}
ALL_PREFIXES = {**DECLARABLE, **BUILTIN}

# ========================= VOCABULARY USED IN ROWS ===========================
# Relation labels, as written by hpo_crosslink_extractor.py, -> relation IRIs.
# A relation missing here stops the script instead of producing a wrong ID.
RELATIONS = {
    "characteristic of": OBO + "RO_0000052",
    "characteristic of part of": OBO + "RO_0002314",
    "part of": OBO + "BFO_0000050",
    "has part": OBO + "BFO_0000051",
    "occurs in": OBO + "BFO_0000066",
    "towards": OBO + "RO_0002503",
    "attached to": OBO + "RO_0002371",
    "located in": OBO + "RO_0001025",
    "produced by": OBO + "RO_0003001",
    "BSPO_0000120": OBO + "BSPO_0000120",   # unlabelled in hp.owl
    "BSPO_0000121": OBO + "BSPO_0000121",
}
RELATION_LABELS = {OBO + "BSPO_0000120": "in left side of",
                   OBO + "BSPO_0000121": "in right side of"}

# Match_Type (propagation output) -> the property whose value matched
MATCH_FIELD = {"label": BUILTIN["rdfs"] + "label",
               "exact_synonym": DECLARABLE["oboInOwl"] + "hasExactSynonym",
               "relational_adjective": OBO + "UBPROP_0000007"}

SEMAPV = BUILTIN["semapv"]
JUSTIFICATION_DIRECT = SEMAPV + "UnspecifiedMatching"
JUSTIFICATION_PROPAGATED = SEMAPV + "CompositeMatching"
# Text normalisation applied by label_in_label() in hpo_ancestor_propagation.py
PREPROCESSING = [SEMAPV + "CaseNormalization", SEMAPV + "PunctuationElemination",
                 SEMAPV + "Tokenization"]

# ======================= SSSOM MODEL (slot tables) ===========================
# Order of the "Slots" tables of the MappingSet and Mapping classes; canonical
# SSSOM/TSV writes metadata slots and columns in exactly this order.
SET_SLOT_ORDER = [
    "sssom_version", "curie_map", "mapping_set_id", "mapping_set_version",
    "mapping_set_source", "mapping_set_title", "mapping_set_description",
    "mapping_set_confidence", "creator_id", "creator_label", "license",
    "subject_type", "subject_source", "subject_source_version", "object_type",
    "object_source", "object_source_version", "predicate_type", "mapping_provider",
    "cardinality_scope", "mapping_tool", "mapping_tool_id", "mapping_tool_version",
    "mapping_date", "publication_date", "subject_match_field", "object_match_field",
    "subject_preprocessing", "object_preprocessing", "similarity_measure",
    "curation_rule", "curation_rule_text", "see_also", "issue_tracker", "other",
    "comment", "extension_definitions",
]
MAPPING_SLOT_ORDER = [
    "record_id", "subject_id", "subject_label", "subject_category", "predicate_id",
    "predicate_label", "predicate_modifier", "object_id", "object_label",
    "object_category", "mapping_justification", "author_id", "author_label",
    "reviewer_id", "reviewer_label", "creator_id", "creator_label", "license",
    "subject_type", "subject_source", "subject_source_version", "object_type",
    "object_source", "object_source_version", "predicate_type", "mapping_provider",
    "mapping_source", "mapping_cardinality", "cardinality_scope", "mapping_tool",
    "mapping_tool_id", "mapping_tool_version", "mapping_date", "publication_date",
    "review_date", "confidence", "reviewer_agreement", "curation_rule",
    "curation_rule_text", "subject_match_field", "object_match_field", "match_string",
    "subject_preprocessing", "object_preprocessing", "similarity_score",
    "similarity_measure", "see_also", "issue_tracker_item", "derived_from", "other",
    "comment",
]
# Slots holding a list: '|'-separated in the TSV block, block sequences in YAML
MULTIVALUED = {"creator_id", "creator_label", "author_id", "author_label", "reviewer_id",
               "reviewer_label", "cardinality_scope", "curation_rule", "curation_rule_text",
               "subject_match_field", "object_match_field", "match_string",
               "subject_preprocessing", "object_preprocessing", "see_also",
               "derived_from", "mapping_set_source"}
# Slots that may move between set level and mapping level (condensation)
PROPAGATABLE = {"curation_rule", "curation_rule_text", "cardinality_scope", "mapping_date",
                "mapping_provider", "mapping_tool", "mapping_tool_version",
                "object_match_field", "object_preprocessing", "object_source",
                "object_source_version", "object_type", "subject_match_field",
                "subject_preprocessing", "subject_source", "subject_source_version",
                "subject_type", "predicate_type", "similarity_measure"}


# ============================== IDENTIFIERS ==================================
class CurieContractor:
    """Turn IRIs into CURIEs using declared prefixes only.

    The longest matching IRI prefix wins (HP_ before the generic obo prefix).
    Every prefix used is remembered, so the curie_map can list exactly those.
    The spec discourages guessing a prefix from the shape of an IRI, so an IRI
    without a declared prefix stops the script."""

    def __init__(self, prefixes: dict[str, str]):
        self.by_length = sorted(prefixes.items(), key=lambda kv: -len(kv[1]))
        self.used: set[str] = set()

    def __call__(self, iri: str) -> str:
        for name, base in self.by_length:
            if iri.startswith(base) and len(iri) > len(base):
                self.used.add(name)
                return f"{name}:{iri[len(base):]}"
        raise SystemExit(f"No declared prefix for IRI {iri!r}; add one to DECLARABLE")


# ================================ ROWS ======================================
def build_rows(df: pd.DataFrame, to_curie: CurieContractor) -> pd.DataFrame:
    """Select the rows to publish and translate each into an SSSOM mapping.

    df is one reviewed propagation table (UBERON or CL). Returns a table with
    one column per Mapping slot (empty string = no value)."""
    keep = df[((df.Link_Type == "direct") | (df.Review_Verdict == "ok"))
              & ~df.Negated.astype(bool)]
    unknown = set(keep.Property) - set(RELATIONS)
    if unknown:
        raise SystemExit(f"Unknown relation(s), add them to RELATIONS: {sorted(unknown)}")

    preprocessing = "|".join(to_curie(p) for p in PREPROCESSING)
    rows = []
    for r in keep.itertuples():
        pred = RELATIONS[r.Property]
        path_note = f"path: {r.Property_Path}"
        if r.In_Union:
            path_note += "; from an OR (union) in the definition"
        row = {
            "subject_id": to_curie(r.HPO_IRI),
            "subject_label": r.HPO_Label,
            "predicate_id": to_curie(pred),
            "predicate_label": RELATION_LABELS.get(pred, r.Property),
            "object_id": to_curie(r.Filler_IRI),
            "object_label": r.Filler_Label,
        }
        if r.Link_Type == "direct":
            row["mapping_justification"] = to_curie(JUSTIFICATION_DIRECT)
            row["comment"] = (f"direct: target appears in the HPO term's own logical "
                              f"definition ({r.Axiom_Type} axiom); {path_note}")
        else:
            distance = int(r.Distance)
            row["mapping_justification"] = to_curie(JUSTIFICATION_PROPAGATED)
            row["subject_match_field"] = to_curie(MATCH_FIELD["label"])
            row["object_match_field"] = to_curie(MATCH_FIELD[r.Match_Type])
            row["match_string"] = r.Matched_Text
            row["subject_preprocessing"] = preprocessing
            row["object_preprocessing"] = preprocessing
            row["comment"] = (
                f"propagated from {to_curie(r.Source_HPO_IRI)} '{r.Source_HPO_Label}' "
                f"({distance} level{'s' if distance != 1 else ''} up), the nearest HPO "
                f"ancestor whose linked target is named in the phenotype label; relation "
                f"taken from that ancestor's definition; {path_note}")
        rows.append(row)

    out = pd.DataFrame(rows)
    for slot in MAPPING_SLOT_ORDER:
        if slot not in out:
            out[slot] = None
    out = out.fillna("")

    # One mapping per (subject, predicate, object, justification); keep all paths
    key = ["subject_id", "predicate_id", "object_id", "mapping_justification"]
    how = {slot: "first" for slot in MAPPING_SLOT_ORDER if slot not in key}
    how["comment"] = lambda s: " | ".join(dict.fromkeys(s))
    out = out.groupby(key, sort=False).agg(how).reset_index()
    return out[MAPPING_SLOT_ORDER]


def escape_multivalued(value: str) -> str:
    """Escape '\\' and '|' inside a free-text value of a multi-valued slot
    (spec rule), so that '|' keeps its meaning as the value separator."""
    return value.replace("\\", "\\\\").replace("|", "\\|")


# ======================= METADATA BLOCK (YAML) ===============================
_YAML_SPECIAL_START = set("-?:,[]{}#&*!|>'\"%@`")
_YAML_KEYWORDS = {"true", "false", "null", "~", "yes", "no", "on", "off", ""}
_YAML_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$|^0x[0-9a-fA-F]+$"
                          r"|^0o[0-7]+$|^[-+]?\.(inf|Inf|INF)$|^\.(nan|NaN|NAN)$")


def yaml_scalar(value) -> str:
    """Write a scalar in YAML plain style when that is unambiguous, otherwise
    double-quoted (canonical rule). Single quotes are never used."""
    s = str(value)
    plain_ok = (s and s[0] not in _YAML_SPECIAL_START and s[0] != " " and s[-1] != " "
                and ": " not in s and " #" not in s and not s.endswith(":")
                and "\n" not in s and "\t" not in s
                and s.lower() not in _YAML_KEYWORDS and not _YAML_NUMBER.match(s))
    if plain_ok:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def metadata_lines(meta: dict) -> list[str]:
    """The metadata block: one '#'-prefixed line per YAML line, slots in the
    order of the MappingSet slot table, slots without a value left out."""
    lines = []
    for slot in SET_SLOT_ORDER:
        value = meta.get(slot)
        if value in (None, "", [], {}):
            continue
        if slot == "curie_map":
            lines.append("curie_map:")
            lines += [f"  {name}: {yaml_scalar(value[name])}" for name in sorted(value)]
        elif slot in MULTIVALUED:
            lines.append(f"{slot}:")                      # block sequence, even for
            lines += [f"  - {yaml_scalar(v)}" for v in value]  # a single value
        else:
            lines.append(f"{slot}: {yaml_scalar(value)}")
    return ["#" + line for line in lines]                 # no space after '#'


# ================================ WRITING ====================================
def write_sssom(rows: pd.DataFrame, meta: dict, path: Path) -> pd.DataFrame:
    """Condense, order, sort and write one SSSOM/TSV file. Returns the rows as
    written (useful for summaries and tests)."""
    rows = rows.copy()

    # Condensation: a propagatable slot with one shared value moves to the set
    for slot in MAPPING_SLOT_ORDER:
        if (slot in PROPAGATABLE and slot not in meta
                and (rows[slot] != "").all() and rows[slot].nunique() == 1):
            value = rows[slot].iloc[0]
            meta[slot] = value.split("|") if slot in MULTIVALUED else value
            rows[slot] = ""

    # No empty columns; columns stay in slot-table order
    rows = rows[[slot for slot in MAPPING_SLOT_ORDER if (rows[slot] != "").any()]]

    # Escape free-text values inside multi-valued slots
    if "match_string" in rows:
        rows["match_string"] = rows["match_string"].map(escape_multivalued)

    # Canonical order: sort on all columns, left to right
    rows = rows.sort_values(list(rows.columns), kind="mergesort").reset_index(drop=True)

    with open(path, "w", encoding="utf-8", newline="\n") as f:   # UTF-8, no BOM, LF
        f.write("\n".join(metadata_lines(meta)) + "\n")
        rows.to_csv(f, sep="\t", index=False, lineterminator="\n")  # quotes only if needed
    return rows


def set_metadata(target: str, name: str, n: int, run: dict, orcid: str,
                 to_curie: CurieContractor) -> dict:
    """Set-level metadata for one target ontology, from run_metadata.json."""
    onts, settings = run["ontologies"], run["settings"]
    texts = (["label"] + (["exact synonym"] if settings.get("use_synonyms") else [])
             + (["relational adjective"] if settings.get("use_adjectives") else []))
    return {
        "mapping_set_id": f"{MAPPING_SET_BASE}hp-{target}.sssom.tsv",
        "mapping_set_version": dt.date.today().isoformat(),
        "mapping_set_title": f"HPO to {name} links from HPO logical definitions",
        "mapping_set_description": (
            f"{n} links from HPO phenotypes to {name} terms. Direct links: the {name} "
            "term appears in the HPO term's own logical definition. Propagated links, for "
            "HPO terms without a direct link: the link of the nearest HPO ancestor is used "
            f"when the target's {', '.join(texts[:-1]) + ' or ' + texts[-1] if len(texts) > 1 else texts[0]} "
            "appears as whole words in the phenotype label (case-insensitive, punctuation "
            "ignored, plural -s/-es allowed on the last word); only propagated links judged "
            "plausible in an automated review are included. predicate_id is the relation "
            "directly before the target in the HPO definition, a shorthand for the full "
            "relation path given in each comment."),
        "creator_id": [to_curie("https://orcid.org/" + orcid)],
        "license": LICENSE,
        "subject_type": "owl class",
        "subject_source": to_curie(OBO + "hp.owl"),
        "subject_source_version": onts["hpo"]["version_iri"],
        "object_type": "owl class",
        "object_source": to_curie(OBO + f"{target}.owl"),
        "object_source_version": onts[target]["version_iri"],
        "mapping_provider": OBO + "hp.owl",
        "mapping_tool": ("hpo_crosslink_extractor.py + hpo_ancestor_propagation.py"
                         + (" --use-synonyms" if settings.get("use_synonyms") else "")
                         + (" --use-adjectives" if settings.get("use_adjectives") else "")),
        "mapping_date": run["created"][:10],   # when the propagation run produced the links
    }


# ================================== MAIN =====================================
def normalise_orcid(raw: str) -> str:
    """Accept 0000-..., orcid:0000-... or https://orcid.org/0000-...; check the format."""
    orcid = raw.strip().removeprefix("https://orcid.org/").removeprefix("orcid:")
    if not ORCID_RE.match(orcid):
        raise SystemExit(f"Not a valid ORCID iD: {raw!r} (expected XXXX-XXXX-XXXX-XXXX)")
    if orcid == PLACEHOLDER_ORCID:
        print("NOTE: creator_id is the placeholder orcid:0000-0000-0000-0000; "
              "pass --creator-orcid with your ORCID iD")
    return orcid


def main():
    ap = argparse.ArgumentParser(
        description="Write the reviewed HPO -> UBERON / CL links as SSSOM/TSV mapping sets")
    ap.add_argument("--reviewed-dir", required=True,
                    help="folder with hpo_to_{uberon,cl}_with_propagation_reviewed.csv")
    ap.add_argument("--metadata", required=True,
                    help="run_metadata.json of the propagation run behind those tables")
    ap.add_argument("--out", default="./data/outputs/sssom", help="output folder")
    ap.add_argument("--creator-orcid", default=PLACEHOLDER_ORCID, metavar="XXXX-XXXX-XXXX-XXXX",
                    help="ORCID iD of the person publishing the set (creator_id)")
    args = ap.parse_args()

    orcid = normalise_orcid(args.creator_orcid)
    run = json.loads(Path(args.metadata).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for target, name in (("uberon", "Uberon"), ("cl", "Cell Ontology")):
        to_curie = CurieContractor(ALL_PREFIXES)
        table = pd.read_csv(Path(args.reviewed_dir) / f"hpo_to_{target}_with_propagation_reviewed.csv")
        rows = build_rows(table, to_curie)
        meta = set_metadata(target, name, len(rows), run, orcid, to_curie)
        # curie_map: declared (non-built-in) prefixes that are actually used
        meta["curie_map"] = {p: DECLARABLE[p] for p in to_curie.used if p not in BUILTIN}
        path = out / f"hp-{target}.sssom.tsv"
        written = write_sssom(rows, meta, path)
        n_direct = (written.mapping_justification == "semapv:UnspecifiedMatching").sum()
        print(f"{path}: {len(written):,} mappings ({n_direct:,} direct, "
              f"{len(written) - n_direct:,} propagated)")


if __name__ == "__main__":
    main()