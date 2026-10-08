# ============ HPO -> UBERON/CL: propagate from the nearest matching ancestor ============
"""
For HPO terms that have no direct UBERON (or CL) link in their logical
definition, climb the HPO hierarchy and borrow the link from the nearest
ancestor that
  1. has a direct UBERON (or CL) link, and
  2. whose linked UBERON/CL label appears as whole words in the query HPO label.

    Tongue nodules            (no UBERON link)
      -> ...
      -> Abnormality of the tongue  -> UBERON "tongue"   "tongue" in "Tongue nodules" -> use it

Condition 2 keeps the climb inside the same organ / cell type: an ancestor such
as "Abnormality of the head" (-> head) is not accepted for "Tongue nodules",
and the climb carries on upwards until a match is found or the root is reached.

Rules
-----
* Nearest first: ancestors are visited level by level (shortest path). The
  first level containing a match wins; all matches on that level are kept
  (a term can have two parents in different branches).
* Whole-word matching, case-insensitive, with simple plurals
  ("tongue" matches "Tongue nodules"; "ear" does NOT match "Hearing abnormality").
* UBERON and CL are handled independently.
* Negated links (owl:complementOf) are never used.
* Only terms under "Phenotypic abnormality" (HP:0000118) are queried.

Optional extra match texts (the label is always used)
-----------------------------------------------------
--use-synonyms    UBERON/CL exact synonyms          "CSF"   -> cerebrospinal fluid
--use-adjectives  UBERON/CL relational adjectives   "renal" -> kidney
                  (annotation has_relational_adjective, UBPROP:0000007)
Match_Type records which one matched: label | exact_synonym | relational_adjective.
Some adjectives are ambiguous ("cervical" = neck or uterine cervix), but a match
only counts against terms linked to the query's own ancestors, which keeps
this in check. Review relational_adjective rows like synonym rows.

Requires hpo_crosslink_extractor.py in the same folder.

Outputs (per ontology, X = uberon / cl)
-------
hpo_to_X_with_propagation.csv  direct links + propagated links (Link_Type column)
hpo_X_unresolved.csv           query terms for which no matching ancestor was found
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
from rdflib import URIRef
from rdflib import Namespace

from hpo_crosslink_extractor import Extractor, load_ontology
from ontology_sources import (add_ontology_args, add_output_args,
                              make_run_dir, finish_run, resolve_all,
                              write_run_metadata)

OBO = "http://purl.obolibrary.org/obo/"
PHENOTYPIC_ABNORMALITY = URIRef(OBO + "HP_0000118")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
HAS_RELATIONAL_ADJECTIVE = URIRef(OBO + "UBPROP_0000007")


# -------------------- LABEL MATCHING --------------------
def _tokens(text: str) -> list[str]:
    """Lower-case words; punctuation and hyphens become separators."""
    return re.findall(r"[a-z0-9]+", text.lower())


def label_in_label(term_label: str, hpo_label: str) -> bool:
    """True if term_label occurs in hpo_label as a run of whole words.

    The last word may carry a plural ending: tongue -> tongues, lens -> lenses.
    """
    t, h = _tokens(term_label), _tokens(hpo_label)
    if not t or len(t) > len(h):
        return False
    *head, last = t
    last_ok = {last, last + "s", last + "es"}
    for i in range(len(h) - len(t) + 1):
        if h[i:i + len(head)] == head and h[i + len(head)] in last_ok:
            return True
    return False


# -------------------- PROPAGATION --------------------
class Propagator:
    def __init__(self, extractor: Extractor, use_synonyms: bool = False,
                 use_adjectives: bool = False):
        self.ex = extractor
        self.hpo = extractor.hpo
        self.use_synonyms = use_synonyms
        self.use_adjectives = use_adjectives
        self.root = PHENOTYPIC_ABNORMALITY
        self._cache: dict[tuple[str, URIRef], list[str]] = {}

    def _annotation_values(self, iri: str, ontology: str, prop: URIRef) -> list[str]:
        """Values of an annotation on a CL/UBERON term, from the loaded target
        ontology and HPO's imported copy (cached)."""
        key = (iri, prop)
        if key not in self._cache:
            values = set()
            for g in (self.ex.targets.get(ontology), self.hpo):
                if g is not None:
                    values |= {str(v) for v in g.objects(URIRef(iri), prop)}
            self._cache[key] = sorted(values)
        return self._cache[key]

    # --- texts a term may be matched by: label (+ synonyms, + adjectives) ---
    def match_texts(self, iri: str, ontology: str) -> list[tuple[str, str]]:
        texts = [("label", self.ex.label(iri, ontology))]
        if self.use_synonyms:
            texts += [("exact_synonym", s)
                      for s in self._annotation_values(iri, ontology, OIO.hasExactSynonym)]
        if self.use_adjectives:
            texts += [("relational_adjective", a)
                      for a in self._annotation_values(iri, ontology, HAS_RELATIONAL_ADJECTIVE)]
        return texts

    def query_terms(self) -> list[URIRef]:
        """HPO terms to consider: Phenotypic abnormality and its descendants."""
        return [c for c in self.ex.hpo_classes
                if c == self.root or self.root in self.ex.hpo_ancestors(c)]

    def ancestors_by_level(self, cls):
        """Yield lists of ancestors: parents, then grandparents, ... (shortest path)."""
        seen = {cls}
        level = [p for p in self.ex.hpo_parents(cls)]
        while level:
            level = sorted({p for p in level if p not in seen}, key=str)
            if not level:
                return
            seen.update(level)
            yield level
            level = [gp for p in level for gp in self.ex.hpo_parents(p)]

    def run(self, direct: pd.DataFrame, ontology: str):
        """direct: the direct-links table for one ontology (from Extractor.run(False))."""
        usable = direct[~direct["Negated"].astype(bool)]
        by_hpo = {h: grp for h, grp in usable.groupby("HPO_IRI")}

        propagated, unresolved = [], []
        for cls in self.query_terms():
            hpo_iri = str(cls)
            if hpo_iri in by_hpo:
                continue  # has its own link, nothing to do
            hpo_label = self.ex.label(cls)

            found = False
            for distance, level in enumerate(self.ancestors_by_level(cls), start=1):
                for anc in level:
                    links = by_hpo.get(str(anc))
                    if links is None:
                        continue
                    for _, link in links.iterrows():
                        for match_type, text in self.match_texts(link["Filler_IRI"], ontology):
                            if label_in_label(text, hpo_label):
                                propagated.append({
                                    **link.to_dict(),
                                    "HPO_IRI": hpo_iri,
                                    "HPO_Label": hpo_label,
                                    "Link_Type": "propagated",
                                    "Source_HPO_IRI": str(anc),
                                    "Source_HPO_Label": self.ex.label(anc),
                                    "Distance": distance,
                                    "Matched_Text": text,
                                    "Match_Type": match_type,
                                })
                                found = True
                                break  # one matching text per link is enough
                if found:
                    break  # nearest level wins
            if not found:
                unresolved.append({"HPO_IRI": hpo_iri, "HPO_Label": hpo_label})

        prop_df = pd.DataFrame(propagated)
        if not prop_df.empty:
            prop_df = (prop_df.sort_values(["HPO_IRI", "Filler_IRI", "Source_HPO_IRI"],
                                           kind="mergesort")
                              .drop_duplicates(["HPO_IRI", "Filler_IRI"]))
        direct_out = direct.assign(Distance=0, Matched_Text=None, Match_Type=None)
        combined = pd.concat([direct_out, prop_df], ignore_index=True)
        return combined.reset_index(drop=True), pd.DataFrame(
            unresolved, columns=["HPO_IRI", "HPO_Label"])


# -------------------- MAIN --------------------
def main():
    ap = argparse.ArgumentParser(description="Propagate UBERON/CL links from matching HPO ancestors")
    add_ontology_args(ap)  # --hpo/--uberon/--cl, --*-release, --latest, --cache-dir
    add_output_args(ap, "./data/outputs/ancestor_propagation")  # --out, --no-timestamp
    ap.add_argument("--use-synonyms", action="store_true",
                    help="also accept a match on UBERON/CL exact synonyms")
    ap.add_argument("--use-adjectives", action="store_true",
                    help="also accept a match on UBERON/CL relational adjectives (renal -> kidney)")
    args = ap.parse_args()

    out = make_run_dir(args.out, timestamped=not args.no_timestamp)

    # Pick local files or download releases, then record what is used
    files = resolve_all(args)
    write_run_metadata(out, files, script=Path(__file__).name, args=args, settings={
        "query_root": "HP:0000118 (Phenotypic abnormality)",
        "use_synonyms": args.use_synonyms,
        "use_adjectives": args.use_adjectives,
    })

    hpo_g = load_ontology(files["hpo"][0])
    cl_g = load_ontology(files["cl"][0]) if files["cl"][0] else None
    uberon_g = load_ontology(files["uberon"][0]) if files["uberon"][0] else None

    ex = Extractor(hpo_g, cl_g, uberon_g)
    direct_uberon, direct_cl = ex.run(follow_hierarchy=False)

    prop = Propagator(ex, use_synonyms=args.use_synonyms,
                      use_adjectives=args.use_adjectives)

    for name, direct in (("uberon", direct_uberon), ("cl", direct_cl)):
        combined, unresolved = prop.run(direct, name.upper())
        combined.to_csv(out / f"hpo_to_{name}_with_propagation.csv", index=False)
        unresolved.to_csv(out / f"hpo_{name}_unresolved.csv", index=False)
        n_prop = combined.loc[combined.Link_Type == "propagated", "HPO_IRI"].nunique()
        print(f"{name.upper()}: {combined.loc[combined.Link_Type == 'direct', 'HPO_IRI'].nunique():,} "
              f"terms direct, {n_prop:,} propagated, {len(unresolved):,} unresolved")

    finish_run(out)  # only now does 'latest' point to this run


if __name__ == "__main__":
    main()