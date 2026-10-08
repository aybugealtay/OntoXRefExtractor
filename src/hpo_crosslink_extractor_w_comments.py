# ##########################################################################
# PLAIN-LANGUAGE GUIDE (comments added for explanation; code is unchanged)
# ##########################################################################
#
# WHAT THIS SCRIPT DOES, IN ONE SENTENCE
#   It reads the Human Phenotype Ontology (HPO) and finds, for every
#   phenotype, which body parts (UBERON) and cell types (CL) it is about.
#
# WHY THAT IS POSSIBLE
#   Many HPO terms come with a small "logical definition" (a formula) written
#   by the HPO curators. For example, "Abnormality of the tongue" is defined
#   roughly as:
#       "has a part that is abnormal AND that abnormality sits in the TONGUE"
#   The tongue in that formula is a UBERON term. This script reads every
#   formula and pulls out every UBERON or CL term mentioned inside it.
#
# INPUTS
#   hp.owl      the HPO ontology file
#   uberon.owl  the anatomy ontology (used for names and obsolete-term checks)
#   cl.owl      the cell ontology (same purpose)
#   Which copy of each file is used is decided by ontology_sources.py:
#   by default the newest release in data/<ontology>/releases/<date>/.
#
# OUTPUTS (in a new folder per run: data/outputs/crosslink_extractor/<date_time>/,
#          and data/outputs/crosslink_extractor/latest points to the newest)
#   run_metadata.json                        which ontology versions, settings
#                                            and software this run used
#   hpo_to_uberon_links_witho_hierarchy.csv  only "direct" links
#   hpo_to_cl_links_witho_hierarchy.csv      only "direct" links
#   hpo_to_uberon_links.csv                  direct + "inherited" links
#   hpo_to_cl_links.csv                      direct + "inherited" links
#
#   DIRECT link    = the anatomy term is written in the phenotype's OWN formula.
#   INHERITED link = the phenotype has no such formula entry itself, but one
#                    of its parent/grandparent phenotypes in HPO does, so it
#                    gets that link too. (This is broad: every child of
#                    "Abnormality of the head" inherits "head".)
#
# EXTRA QUALITY INFORMATION KEPT FOR EACH LINK
#   - In_Union : the term appeared inside an "OR" (e.g. "kidney OR liver"),
#                so the link is only one of several possibilities.
#   - Negated  : the term appeared inside a "NOT", so it is explicitly NOT
#                involved. These should not be treated as real links.
#   - Obsolete : the anatomy/cell term has been retired; the script also
#                looks up what replaced it.
#   - Found_In_Target : whether the term exists in the loaded UBERON/CL file.
# ##########################################################################

# ================= HPO -> UBERON / CL Extractor (revised) =================
"""
Extract links from Human Phenotype Ontology (HPO) classes to Uberon (anatomy)
and Cell Ontology (CL) classes.

A CL/UBERON class is captured wherever it appears inside an HPO class
expression (equivalentClass or anonymous subClassOf axioms), at any depth:

    has_part some (PATO_x and inheres_in some (CL_y and part_of some UBERON_z))
                                               ^^^^                  ^^^^^^^^
    both CL_y (an intersection operand) and UBERON_z (a restriction filler)
    are reported, together with the property path that leads to them.

Hierarchy handling
------------------
Direct links are computed per HPO class without ever descending into the
axioms of named classes (PATO, CL, UBERON, GO, ... are leaves). Inherited
links are then added by walking only the HPO is_a hierarchy
(named HP_ superclasses), so axioms of imported ontologies never leak in.

Output columns
--------------
HPO_IRI, HPO_Label        the phenotype the row is about
Link_Type                 "direct" or "inherited"
Source_HPO_IRI/_Label     the HPO class whose axiom contains the link
Axiom_Type                equivalentClass | subClassOf
Property                  innermost property (e.g. "inheres in", "part of")
Property_Path             full path, e.g. "has part some > inheres in some"
Filler_Role               restriction_filler | intersection_operand | union_operand
In_Union                  True if the term sits under an owl:unionOf (disjunctive!)
Negated                   True if the term sits under an owl:complementOf
Filler_IRI, Filler_Label
Obsolete                  True if the term is deprecated
Replacement_IRI/_Label    final replacement (chains A -> B -> C are followed)
Replacement_Obsolete      True if the chain ends at a term that is itself obsolete
Consider_IRIs             oboInOwl:consider suggestions, "|"-separated
Found_In_Target           False if the IRI is not declared in the loaded CL/UBERON

Note: a path containing "only" (allValuesFrom) is a restriction, not a claim
that the phenotype involves the term; filter on Property_Path if needed.
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

import pandas as pd
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

# Helpers from ontology_sources.py (same folder): choosing/downloading the
# ontology files, making the run folder, and writing run_metadata.json.
from ontology_sources import (add_ontology_args, add_output_args,
                              make_run_dir, finish_run, resolve_all,
                              write_run_metadata)

# -------------------- CONSTANTS --------------------
# Every ontology term is identified by a web address (IRI). HPO terms start
# with ".../obo/HP_", anatomy terms with ".../obo/UBERON_", cell types with
# ".../obo/CL_". The script uses these prefixes to recognise which ontology a
# term belongs to.
OBO = "http://purl.obolibrary.org/obo/"
HP_PREFIX = OBO + "HP_"
TARGET_PREFIXES = {"CL": OBO + "CL_", "UBERON": OBO + "UBERON_"}

OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
IAO_REPLACED_BY = URIRef(OBO + "IAO_0100001")  # "term replaced by"

# Restriction predicates whose object is (or contains) a class / individual
# In plain words: the different ways a formula can point to another term.
#   "some"  = involves at least one (the usual case: "inheres in SOME tongue")
#   "only"  = may only involve (a constraint, weaker evidence of a link)
#   "value" = points to one specific thing
#   "qualified-cardinality" = "exactly/at least N of" a term
RESTRICTION_FILLERS = [
    (OWL.someValuesFrom, "some"),
    (OWL.allValuesFrom, "only"),
    (OWL.hasValue, "value"),
    (OWL.onClass, "qualified-cardinality"),
]

# The columns of the output tables (see the list in the docstring above).
COLUMNS = [
    "HPO_IRI", "HPO_Label", "Link_Type", "Source_HPO_IRI", "Source_HPO_Label",
    "Axiom_Type", "Property", "Property_Path", "Filler_Role", "In_Union",
    "Negated", "Filler_IRI", "Filler_Label", "Obsolete", "Replacement_IRI",
    "Replacement_Label", "Replacement_Obsolete", "Consider_IRIs", "Found_In_Target",
]
# Two rows count as duplicates when these columns are all equal.
DEDUP_KEYS = ["HPO_IRI", "Property_Path", "Filler_IRI", "In_Union", "Negated"]


# -------------------- HELPERS --------------------
# Small utility functions used throughout the script.

# Reads an ontology file (.owl) into memory as a graph of triples.
def load_ontology(path_or_url: str) -> Graph:
    """Load an ontology; rdflib guesses the format from the extension."""
    g = Graph()
    fmt = "xml" if str(path_or_url).endswith((".owl", ".rdf")) else None
    g.parse(path_or_url, format=fmt)
    print(f"Loaded {len(g):,} triples from {path_or_url}")
    return g


# Answers "is this term from CL, from UBERON, or neither?"
def target_of(iri: str) -> str | None:
    """Return 'CL' / 'UBERON' if the IRI belongs to that ontology (strict prefix)."""
    for name, prefix in TARGET_PREFIXES.items():
        if iri.startswith(prefix):
            return name
    return None


# Replacement terms are sometimes written in short form ("CL:0000001") and
# sometimes as full web addresses. This turns both into the full address.
def to_iri(value) -> str:
    """Normalise replacement values, which may be IRIs or CURIE strings ('CL:0000001')."""
    s = str(value).strip()
    if s.startswith("http"):
        return s
    if ":" in s:
        prefix, local = s.split(":", 1)
        return f"{OBO}{prefix}_{local}"
    return s


# Looks up the human-readable name of a term (e.g. UBERON_0001723 -> "tongue"),
# preferring the English name. If no name is found, falls back to the ID.
def get_label(graphs, term) -> str | None:
    """First English/untagged rdfs:label found in the given graphs, else the IRI fragment."""
    if term is None:
        return None
    for g in graphs:
        if g is None:
            continue
        labels = list(g.objects(term, RDFS.label))
        if labels:
            for lab in labels:
                if isinstance(lab, Literal) and lab.language in (None, "en"):
                    return str(lab)
            return str(labels[0])
    if isinstance(term, URIRef):
        return str(term).rsplit("/", 1)[-1]
    return str(term)


# True if a term has been marked as retired ("deprecated") in the ontology.
def is_deprecated(g: Graph, cls) -> bool:
    return any(str(v).strip().lower() in ("true", "1") for v in g.objects(cls, OWL.deprecated))


# Builds a lookup table of all retired terms: for each one, which term
# replaces it (if any) and which terms the curators suggest considering.
def build_obsolete_map(g: Graph) -> dict:
    """{obsolete_IRI: {"replacement": IRI|None, "consider": [IRI, ...]}}.

    Every deprecated class is included, with or without a replacement.
    """
    out = {}
    for cls in sorted(set(g.subjects(OWL.deprecated, None)), key=str):
        if not is_deprecated(g, cls):
            continue
        repl = g.value(cls, IAO_REPLACED_BY) or g.value(cls, OIO.replacedBy)
        out[str(cls)] = {
            "replacement": to_iri(repl) if repl is not None else None,
            "consider": sorted(to_iri(c) for c in g.objects(cls, OIO.consider)),
        }
    return out


# -------------------- CLASS-EXPRESSION WALKER --------------------
# THE CORE OF THE SCRIPT.
# A logical definition is a nested formula, like a sentence with brackets:
#     has_part SOME ( abnormal AND inheres_in SOME ( tongue ) )
# This function opens every bracket, one level at a time, and reports every
# UBERON or CL term it finds inside. While doing so it remembers:
#   - the "path" of relations it went through to get there
#     (e.g. "has part some > inheres in some"),
#   - whether it passed through an OR  (in_union = True),
#   - whether it passed through a NOT  (negated  = True; two NOTs cancel out).
# It never opens up a NAMED term (e.g. it does not look inside UBERON's own
# definition of "tongue"), so only what HPO itself says is counted.
def walk_expression(g, node, path=(), role="named", in_union=False, negated=False,
                    _depth=0):
    """Yield (term, path, role, in_union, negated) for every CL/UBERON class
    appearing anywhere inside an anonymous class expression.

    Named classes are leaves: their own axioms are NOT followed, so imported
    PATO/CL/UBERON axioms never get attributed to the HPO class.
    """
    # Case 1: we reached a named term. If it is UBERON or CL, report it. Stop.
    if isinstance(node, URIRef):
        if target_of(str(node)):
            yield node, path, role, in_union, negated
        return
    # Anything else that is not an anonymous bracket: nothing to open. Stop.
    # (The depth limit of 100 is a safety net against endless loops.)
    if not isinstance(node, BNode) or _depth > 100:
        return

    # Case 2: a relation such as "inheres_in SOME X". Note the relation and
    # look inside X.
    # Restrictions: onProperty + (some|only|value|onClass)
    prop = g.value(node, OWL.onProperty)
    for pred, quant in RESTRICTION_FILLERS:
        for filler in g.objects(node, pred):
            yield from walk_expression(g, filler, path + ((prop, quant),),
                                       "restriction_filler", in_union, negated,
                                       _depth + 1)

    # Case 3: "A AND B" or "A OR B". Look inside each part. For OR, mark
    # everything found below it as in_union.
    # Boolean constructors
    for pred, member_role in ((OWL.intersectionOf, "intersection_operand"),
                              (OWL.unionOf, "union_operand")):
        for lst in g.objects(node, pred):
            for member in Collection(g, lst):
                yield from walk_expression(g, member, path, member_role,
                                           in_union or pred == OWL.unionOf,
                                           negated, _depth + 1)

    # Case 4: "NOT X". Look inside X, flipping the negated flag.
    for comp in g.objects(node, OWL.complementOf):
        yield from walk_expression(g, comp, path, role, in_union, not negated,
                                   _depth + 1)


# -------------------- EXTRACTION --------------------
# The Extractor holds the loaded ontologies and does the actual work:
# for each HPO term, run the walker above and turn each finding into a row.
class Extractor:
    def __init__(self, hpo_graph, cl_graph=None, uberon_graph=None):
        self.hpo = hpo_graph
        self.targets = {"CL": cl_graph, "UBERON": uberon_graph}

        # Build the list of retired terms. HPO carries an internal copy of
        # some UBERON/CL terms, but the real UBERON/CL files are trusted
        # first, because they are more up to date for their own terms.
        # Obsolescence: a loaded target ontology is authoritative for its own
        # terms; HPO's merged import copy is only a fallback for the others.
        self.obsolete = {}
        for iri, info in build_obsolete_map(hpo_graph).items():
            ont = target_of(iri)
            if ont is None or self.targets.get(ont) is None:
                self.obsolete[iri] = info
        for g in (uberon_graph, cl_graph):
            if g is not None:
                self.obsolete.update(build_obsolete_map(g))

        self._direct = None
        # The list of HPO terms to process: every HPO class that is not retired.
        self.hpo_classes = [
            c for c in sorted(set(hpo_graph.subjects(RDF.type, OWL.Class)), key=str)
            if isinstance(c, URIRef) and str(c).startswith(HP_PREFIX)
            and not is_deprecated(hpo_graph, c)
        ]
        self._labels = {}

    # If a retired term was replaced by another retired term, keep following
    # the chain (A -> B -> C) until reaching a current term or a dead end.
    # ---- final replacement, following chains (A -> B -> C, B obsolete) ----
    def final_replacement(self, iri):
        seen = {iri}
        repl = self.obsolete[iri]["replacement"]
        while repl is not None and repl in self.obsolete and repl not in seen:
            seen.add(repl)
            nxt = self.obsolete[repl]["replacement"]
            if nxt is None:
                break  # chain ends at an obsolete term without replacement
            repl = nxt
        return repl

    # Name lookup with a memory (cache), so each name is only looked up once.
    # ---- labels (cached) ----
    def label(self, term, ontology=None):
        key = str(term)
        if key not in self._labels:
            graphs = [self.targets.get(ontology), self.hpo]
            self._labels[key] = get_label(graphs, URIRef(key))
        return self._labels[key]

    # Readable name of a relation, e.g. "part of". Handles reversed
    # relations such as "inverse(part of)" = "has part".
    def prop_label(self, p):
        if p is None:
            return "?"
        if isinstance(p, BNode):  # anonymous property, e.g. inverseOf(part of)
            inv = self.hpo.value(p, OWL.inverseOf)
            return f"inverse({self.prop_label(inv)})" if inv is not None else "?"
        return self.label(p)

    # Turns the remembered path into text: "has part some > inheres in some".
    def path_str(self, path):
        return " > ".join(f"{self.prop_label(p)} {q}" for p, q in path)

    # DIRECT LINKS for one HPO term: look at each of its formulas (both the
    # full definition "equivalentClass" and partial statements "subClassOf"),
    # run the walker, and make one row per UBERON/CL term found.
    # Plain "X is a kind of Y" statements between HPO terms are skipped here;
    # they are the HPO hierarchy and are handled further down.
    # ---- direct links of one HPO class ----
    def direct_links(self, cls):
        rows = []
        for axiom_pred, axiom_name in ((OWL.equivalentClass, "equivalentClass"),
                                       (RDFS.subClassOf, "subClassOf")):
            for expr in self.hpo.objects(cls, axiom_pred):
                if isinstance(expr, URIRef):
                    continue  # named superclass = hierarchy, handled separately
                for term, path, role, in_union, negated in walk_expression(self.hpo, expr):
                    rows.append(self._row(cls, term, path, role, in_union,
                                          negated, axiom_name))
        return rows

    # Builds one output row: the phenotype, the anatomy/cell term, how they
    # are connected, and the quality flags (OR, NOT, retired, replacement...).
    def _row(self, cls, term, path, role, in_union, negated, axiom_name):
        iri = str(term)
        ont = target_of(iri)
        target_g = self.targets.get(ont)
        obs = self.obsolete.get(iri)
        repl = self.final_replacement(iri) if obs else None
        return {
            "Ontology": ont,
            "HPO_IRI": str(cls),
            "HPO_Label": self.label(cls),
            "Link_Type": "direct",
            "Source_HPO_IRI": str(cls),
            "Source_HPO_Label": self.label(cls),
            "Axiom_Type": axiom_name,
            "Property": self.prop_label(path[-1][0]) if path else None,
            "Property_Path": self.path_str(path),
            "Filler_Role": role,
            "In_Union": in_union,
            "Negated": negated,
            "Filler_IRI": iri,
            "Filler_Label": self.label(iri, ont),
            "Obsolete": obs is not None,
            "Replacement_IRI": repl,
            "Replacement_Label": self.label(repl, target_of(repl)) if repl else None,
            "Replacement_Obsolete": (repl in self.obsolete) if repl else None,
            "Consider_IRIs": "|".join(obs["consider"]) if obs else None,
            "Found_In_Target": (None if target_g is None
                                else (URIRef(iri), RDF.type, OWL.Class) in target_g),
        }

    # Walking the HPO family tree (only HPO terms, never other ontologies):
    #   hpo_parents   = the direct parents of a term
    #   hpo_ancestors = parents, grandparents, ... all the way to the top
    # ---- HPO-only ancestors ----
    def hpo_parents(self, cls):
        return [p for p in self.hpo.objects(cls, RDFS.subClassOf)
                if isinstance(p, URIRef) and str(p).startswith(HP_PREFIX)]

    def hpo_ancestors(self, cls):
        seen, queue = set(), deque(self.hpo_parents(cls))
        while queue:
            p = queue.popleft()
            if p in seen or p == cls:
                continue
            seen.add(p)
            queue.extend(self.hpo_parents(p))
        return seen

    # MAIN STEP.
    #  1. Compute the direct links of every HPO term (done once, then reused).
    #  2. If follow_hierarchy is on: give every term a copy of ALL its
    #     ancestors' direct links, marked "inherited".
    #  3. Remove duplicates (a direct row is kept over an inherited copy).
    #  4. Split into one UBERON table and one CL table.
    # ---- main entry ----
    def run(self, follow_hierarchy=True):
        if self._direct is None:  # computed once, reused by both modes
            self._direct = {c: self.direct_links(c) for c in self.hpo_classes}
        direct = self._direct
        rows = [r for rs in direct.values() for r in rs]

        if follow_hierarchy:
            for cls in self.hpo_classes:
                for anc in self.hpo_ancestors(cls):
                    for r in direct.get(anc, ()):
                        rows.append({**r, "HPO_IRI": str(cls),
                                     "HPO_Label": self.label(cls),
                                     "Link_Type": "inherited"})

        df = pd.DataFrame(rows, columns=["Ontology"] + COLUMNS)
        # "direct" sorts before "inherited", so direct rows win on duplicates
        df = (df.sort_values(["HPO_IRI", "Link_Type", "Filler_IRI", "Property_Path",
                              "Source_HPO_IRI", "Axiom_Type", "Filler_Role"],
                             kind="mergesort")
                .drop_duplicates(subset=DEDUP_KEYS)
                .reset_index(drop=True))
        split = {k: df[df["Ontology"] == k][COLUMNS].reset_index(drop=True)
                 for k in ("UBERON", "CL")}
        return split["UBERON"], split["CL"]


# Kept so that older code calling extract_hpo_links(...) still works.
def extract_hpo_links(hpo_graph, cl_graph=None, uberon_graph=None,
                      follow_hierarchy=True):
    """Backwards-compatible wrapper: returns (df_uberon, df_cl)."""
    return Extractor(hpo_graph, cl_graph, uberon_graph).run(follow_hierarchy)


# -------------------- MAIN --------------------
# What happens when you run the script from the command line:
#   1. read the options (which ontology files/releases, where to save)
#   2. create a new, time-stamped output folder for this run
#   3. decide which ontology files to use (by default the newest downloaded
#      release, downloading the latest if there is none yet) and write
#      run_metadata.json recording them
#   4. load the three ontologies
#   5. save the direct-only tables
#   6. save the direct + inherited tables
#   7. only now point "latest" at this run, so a run that crashed halfway
#      never becomes "latest"
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_ontology_args(ap)  # --hpo/--uberon/--cl, --*-release, --latest, --cache-dir
    add_output_args(ap, "./data/outputs/crosslink_extractor")  # --out, --no-timestamp
    args = ap.parse_args()

    out = make_run_dir(args.out, timestamped=not args.no_timestamp)

    # Pick local files or download releases, then record what is used
    files = resolve_all(args)
    write_run_metadata(out, files, script=Path(__file__).name, args=args)

    hpo_g = load_ontology(files["hpo"][0])
    cl_g = load_ontology(files["cl"][0]) if files["cl"][0] else None
    uberon_g = load_ontology(files["uberon"][0]) if files["uberon"][0] else None

    ex = Extractor(hpo_g, cl_g, uberon_g)

    u, c = ex.run(follow_hierarchy=False)
    u.to_csv(out / "hpo_to_uberon_links_witho_hierarchy.csv", index=False)
    c.to_csv(out / "hpo_to_cl_links_witho_hierarchy.csv", index=False)
    print(f"direct:   {len(u):,} UBERON rows, {len(c):,} CL rows")

    u, c = ex.run(follow_hierarchy=True)
    u.to_csv(out / "hpo_to_uberon_links.csv", index=False)
    c.to_csv(out / "hpo_to_cl_links.csv", index=False)
    print(f"with hierarchy: {len(u):,} UBERON rows, {len(c):,} CL rows")

    finish_run(out)  # only now does 'latest' point to this run


if __name__ == "__main__":
    main()