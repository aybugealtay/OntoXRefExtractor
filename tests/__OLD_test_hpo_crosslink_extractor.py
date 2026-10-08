
import sys
from pathlib import Path
import pytest
from rdflib import Graph, Namespace, RDF, RDFS, OWL, URIRef, BNode, Literal
import pandas as pd

# Ensure src/ is in import path
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from hpo_crosslink_extractor import (
    get_label,
    build_obsolete_map,
    extract_hpo_links,
)

HP = Namespace("http://purl.obolibrary.org/obo/HP_")
CL = Namespace("http://purl.obolibrary.org/obo/CL_")
UBERON = Namespace("http://purl.obolibrary.org/obo/UBERON_")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")


@pytest.fixture
def simple_graphs():
    hpo_g = Graph()
    cl_g = Graph()

    hpo_term = HP["0000001"]
    cl_term = CL["0000002"]
    prop = URIRef("http://purl.obolibrary.org/obo/RO_0002202")

    # HPO term
    hpo_g.add((hpo_term, RDF.type, OWL.Class))
    hpo_g.add((hpo_term, RDFS.label, Literal("Abnormal cell morphology")))

    # Restriction (HPO -> CL)
    restriction = BNode()
    hpo_g.add((hpo_term, OWL.equivalentClass, restriction))
    hpo_g.add((restriction, OWL.onProperty, prop))
    hpo_g.add((restriction, OWL.someValuesFrom, cl_term))

    # CL term
    cl_g.add((cl_term, RDF.type, OWL.Class))
    cl_g.add((cl_term, RDFS.label, Literal("Neuron")))

    return hpo_g, cl_g


def test_get_label(simple_graphs):
    g, _ = simple_graphs
    hpo_term = list(g.subjects(RDF.type, OWL.Class))[0]
    assert get_label(g, hpo_term) == "Abnormal cell morphology"


def test_build_obsolete_map():
    g = Graph()
    obsolete = CL["0000999"]
    replacement = CL["0000002"]
    g.add((obsolete, RDF.type, OWL.Class))
    g.add((obsolete, OWL.deprecated, Literal(True)))
    g.add((obsolete, OIO.replacedBy, replacement))
    result = build_obsolete_map(g)
    assert result[str(obsolete)] == str(replacement)


def test_extract_hpo_links(simple_graphs):
    hpo_g, cl_g = simple_graphs
    df_uberon, df_cl = extract_hpo_links(hpo_g, cl_graph=cl_g)

    assert isinstance(df_cl, pd.DataFrame)
    assert len(df_cl) == 1

    row = df_cl.iloc[0]
    assert "CL" in row["Filler_IRI"]
    assert row["HPO_Label"] == "Abnormal cell morphology"
    assert row["Filler_Label"] == "Neuron"
