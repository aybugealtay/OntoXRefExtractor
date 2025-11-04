# ================= HPO → UBERON/CL Extractor =================
"""
This script extracts mappings from Human Phenotype Ontology (HPO) terms
to Uberon (body parts) and Cell Ontology (CL) terms.

It can optionally follow hierarchical relationships, meaning that
phenotypes inherit links from parent classes. The script handles:

- Direct HPO → CL/UBERON links
- Nested logical definitions (restrictions, intersections/unions)
- Obsolete terms and their replacements

Output:
- Two pandas DataFrames: one for UBERON, one for CL
"""

from rdflib import Graph, RDF, RDFS, OWL, URIRef, BNode, Literal, Namespace
import pandas as pd

# -------------------- NAMESPACES --------------------
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")  # oboInOwl:replacedBy
IAO = Namespace("http://purl.obolibrary.org/obo/IAO_")  # sometimes replacement info


# -------------------- LOAD ONTOLOGY --------------------
def load_ontology(path_or_url: str) -> Graph:
    """
    Load an OWL ontology file (local or URL) into an RDFLib graph.

    Parameters:
    - path_or_url: str, path or URL to OWL file

    Returns:
    - g: RDFLib Graph containing ontology triples
    """
    g = Graph()
    g.parse(path_or_url, format="xml")
    print(f"Loaded ontology with {len(g)} triples from {path_or_url}")
    return g


# -------------------- GET LABEL --------------------
def get_label(g, term):
    """
    Get a human-readable label for a class or property.

    Parameters:
    - g: RDFLib Graph
    - term: RDFLib URIRef or BNode

    Returns:
    - str: human-readable label, or URI fragment if label missing
    """
    label = g.value(term, RDFS.label)
    if isinstance(label, Literal):
        return str(label)
    if isinstance(term, URIRef):
        return term.split("/")[-1]
    return str(term)


# -------------------- BUILD OBSOLETE MAP --------------------
def build_obsolete_map(ontology_graph):
    """
    Create a map of deprecated terms to their replacements.

    Parameters:
    - ontology_graph: RDFLib Graph

    Returns:
    - dict: {obsolete_IRI: replacement_IRI}
    """
    obsolete_map = {}
    for cls in ontology_graph.subjects(RDF.type, OWL.Class):
        deprecated = ontology_graph.value(cls, OWL.deprecated)
        if deprecated and str(deprecated) == "true":
            replacement = ontology_graph.value(
                cls, OIO.replacedBy
            ) or ontology_graph.value(cls, IAO["0100001"])
            if replacement:
                obsolete_map[str(cls)] = str(replacement)
    return obsolete_map


# -------------------- RECURSIVE RESTRICTION EXTRACTOR --------------------
def extract_restrictions(
    g,
    node,
    records,
    cls,
    hpo_label,
    obsolete_maps,
    cl_graph=None,
    uberon_graph=None,
    visited=None,
    follow_hierarchy=True,
):
    """
    Recursively traverse HPO restrictions and optional inheritance.

    Parameters:
    - g: RDFLib Graph (HPO)
    - node: current node to explore
    - records: dict with "CL" and "UBERON" lists for storing links
    - cls: current HPO class (URI)
    - hpo_label: human-readable label of HPO class
    - obsolete_maps: dict of obsolete terms for CL/UBERON
    - cl_graph, uberon_graph: RDFLib Graphs for CL/UBERON
    - visited: set of nodes already visited (to prevent infinite recursion)
    - follow_hierarchy: bool, whether to follow subclass/equivalentClass links
    """
    if visited is None:
        visited = set()
    if node in visited:
        return
    visited.add(node)

    # --- Handle OWL Restrictions (someValuesFrom / allValuesFrom) ---
    for prop_type, prop_name in [
        (OWL.someValuesFrom, "some"),
        (OWL.allValuesFrom, "only"),
    ]:
        filler = g.value(node, prop_type)
        if filler:
            prop = g.value(node, OWL.onProperty)
            filler_iri = str(filler)
            is_obsolete = False
            replacement_iri = replacement_label = None

            # Check if the filler term is obsolete and get replacement
            if (
                cl_graph
                and "CL" in filler_iri
                and filler_iri in obsolete_maps.get("CL", {})
            ):
                replacement_iri = obsolete_maps["CL"][filler_iri]
                replacement_label = get_label(cl_graph, URIRef(replacement_iri))
                is_obsolete = True
            elif (
                uberon_graph
                and "UBERON" in filler_iri
                and filler_iri in obsolete_maps.get("UBERON", {})
            ):
                replacement_iri = obsolete_maps["UBERON"][filler_iri]
                replacement_label = get_label(uberon_graph, URIRef(replacement_iri))
                is_obsolete = True

            # Store the link
            if "CL" in filler_iri:
                records["CL"].append(
                    {
                        "HPO_IRI": str(cls),
                        "HPO_Label": hpo_label,
                        "Property": get_label(g, prop),
                        "Filler_IRI": filler_iri,
                        "Filler_Label": get_label(cl_graph, URIRef(filler_iri)),
                        "Obsolete": is_obsolete,
                        "Replacement_IRI": replacement_iri,
                        "Replacement_Label": replacement_label,
                    }
                )
            elif "UBERON" in filler_iri:
                records["UBERON"].append(
                    {
                        "HPO_IRI": str(cls),
                        "HPO_Label": hpo_label,
                        "Property": get_label(g, prop),
                        "Filler_IRI": filler_iri,
                        "Filler_Label": get_label(uberon_graph, URIRef(filler_iri)),
                        "Obsolete": is_obsolete,
                        "Replacement_IRI": replacement_iri,
                        "Replacement_Label": replacement_label,
                    }
                )

            # Recursively handle nested anonymous classes
            if isinstance(filler, BNode):
                extract_restrictions(
                    g,
                    filler,
                    records,
                    cls,
                    hpo_label,
                    obsolete_maps,
                    cl_graph,
                    uberon_graph,
                    visited,
                    follow_hierarchy=follow_hierarchy,
                )

    # --- Handle intersections/unions ---
    for coll in [OWL.intersectionOf, OWL.unionOf]:
        collection = g.value(node, coll)
        while collection and collection != RDF.nil:
            first = g.value(collection, RDF.first)
            if first:
                extract_restrictions(
                    g,
                    first,
                    records,
                    cls,
                    hpo_label,
                    obsolete_maps,
                    cl_graph,
                    uberon_graph,
                    visited,
                    follow_hierarchy=follow_hierarchy,
                )
            collection = g.value(collection, RDF.rest)

    # --- Optional: follow hierarchical relationships ---
    if follow_hierarchy:
        for rel in [OWL.equivalentClass, RDFS.subClassOf]:
            for expr in g.objects(node, rel):
                extract_restrictions(
                    g,
                    expr,
                    records,
                    cls,
                    hpo_label,
                    obsolete_maps,
                    cl_graph,
                    uberon_graph,
                    visited,
                    follow_hierarchy=follow_hierarchy,
                )


# -------------------- EXTRACT HPO → CL/UBERON LINKS --------------------
def extract_hpo_links(
    hpo_graph,
    cl_graph=None,
    uberon_graph=None,
    hpo_prefix="http://purl.obolibrary.org/obo/HP_",
    follow_hierarchy=True,
):
    """
    Extract explicit and/or inherited links from HPO to CL/UBERON.

    Parameters:
    - hpo_graph: RDFLib Graph of HPO
    - cl_graph: RDFLib Graph of CL ontology
    - uberon_graph: RDFLib Graph of UBERON ontology
    - hpo_prefix: filter HPO terms by this prefix
    - follow_hierarchy: whether to include inherited links

    Returns:
    - df_uberon: pandas DataFrame of HPO → UBERON links
    - df_cl: pandas DataFrame of HPO → CL links
    """
    obsolete_maps = {}
    if cl_graph:
        obsolete_maps["CL"] = build_obsolete_map(cl_graph)
    if uberon_graph:
        obsolete_maps["UBERON"] = build_obsolete_map(uberon_graph)

    records = {"CL": [], "UBERON": []}

    for cls in hpo_graph.subjects(RDF.type, OWL.Class):
        if str(cls).startswith(hpo_prefix):
            hpo_label = get_label(hpo_graph, cls)
            visited = set()
            for rel in [OWL.equivalentClass, RDFS.subClassOf]:
                for expr in hpo_graph.objects(cls, rel):
                    extract_restrictions(
                        hpo_graph,
                        expr,
                        records,
                        cls,
                        hpo_label,
                        obsolete_maps,
                        cl_graph,
                        uberon_graph,
                        visited,
                        follow_hierarchy=follow_hierarchy,
                    )

    # Remove duplicates in final DataFrames
    df_uberon = (
        pd.DataFrame(records["UBERON"])
        .drop_duplicates(subset=["HPO_IRI", "Property", "Filler_IRI"])
        .reset_index(drop=True)
    )
    df_cl = (
        pd.DataFrame(records["CL"])
        .drop_duplicates(subset=["HPO_IRI", "Property", "Filler_IRI"])
        .reset_index(drop=True)
    )

    return df_uberon, df_cl


# -------------------- MAIN EXECUTION --------------------
hpo_g = load_ontology("./data/hp.owl")
cl_g = load_ontology("./data/cl.owl")  # latest CL
uberon_g = load_ontology("./data/uberon.owl")  # latest UBERON


def main():
    df_uberon_witho_hier, df_cl_witho_hier = extract_hpo_links(
        hpo_g, cl_g, uberon_g, follow_hierarchy=False
    )
    df_uberon_witho_hier.to_csv(
        "./data/outputs/hpo_to_uberon_links_witho_hierarchy.csv", index=False
    )
    df_cl_witho_hier.to_csv(
        "./data/outputs/hpo_to_cl_links_witho_hierarchy.csv", index=False
    )
    df_uberon, df_cl = extract_hpo_links(hpo_g, cl_g, uberon_g, follow_hierarchy=True)
    df_uberon.to_csv("./data/outputs/hpo_to_uberon_links.csv", index=False)
    df_cl.to_csv("./data/outputs/hpo_to_cl_links.csv", index=False)


if __name__ == "__main__":
    main()
