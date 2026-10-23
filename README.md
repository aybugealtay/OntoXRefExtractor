# OntoXRefExtractor

Links phenotypes in the **Human Phenotype Ontology (HPO)** to the anatomy (**Uberon**) and cell types (**Cell Ontology**) they describe, and exports the links as **[SSSOM](https://mapping-commons.github.io/sssom/)** mapping sets.

```
HP:0000157  Abnormality of the tongue  →  UBERON:0001723  tongue
HP:0000199  Tongue nodules             →  UBERON:0001723  tongue        (propagated)
HP:0004444  Spherocytosis              →  CL:0000232      erythrocyte
```

## Features

- **Direct links** extracted from HPO's logical definitions, at any nesting depth
- **Propagation** for terms without a link: borrows the nearest HPO ancestor's link, but only if the anatomy or cell type is named in the term's label
- **Versioned ontology releases**: automatic download, caching, and a `run_metadata.json` with release dates and checksums for every run
- **SSSOM/TSV export**, validated with sssom-py and SSSOM-Java

## Installation

```bash
git clone https://github.com/aybugealtay/OntoXRefExtractor.git
cd OntoXRefExtractor
pip install -r requirements.txt
```

Requires Python 3.10+.

## Usage

Run from the repository root. Ontology files are downloaded on first use.

```bash
# 1. Direct links
python src/hpo_crosslink_extractor.py

# 2. Direct + propagated links
python src/hpo_ancestor_propagation.py --use-synonyms --use-adjectives

# 3. SSSOM export (from reviewed propagation tables)
python src/make_sssom.py \
    --reviewed-dir data/outputs/ancestor_propagation/<run>/claude_reviews \
    --metadata     data/outputs/ancestor_propagation/<run>/run_metadata.json \
    --creator-orcid <your ORCID iD>
```

Choosing ontology releases (same options for `--uberon` and `--cl`):

| Option | Uses |
|---|---|
| *(none)* | newest downloaded release |
| `--hpo-release 2026-09-01` | a specific release |
| `--latest` | the latest releases online |
| `--hpo path/to/hp.owl` | a local file |

Run any script with `--help` for all options.

## Outputs

Each run writes to `data/outputs/<script>/<timestamp>/`, with `latest` pointing to the newest run.

| File | Contents |
|---|---|
| `hpo_to_{uberon,cl}_links*.csv` | direct links (`_witho_hierarchy`) or direct + inherited |
| `hpo_to_{uberon,cl}_with_propagation.csv` | direct + propagated links |
| `hpo_{uberon,cl}_unresolved.csv` | terms still without a link |
| `run_metadata.json` | ontology releases, checksums, settings, software versions |
| `hp-{uberon,cl}.sssom.tsv` | SSSOM mapping sets (`data/outputs/sssom/`) |

The SSSOM files contain all direct links plus the propagated links reviewed as `ok`. The predicate is the RO/BFO relation that precedes the target in the HPO definition (e.g. `RO:0002314`, *characteristic of part of*).

## Results

HPO 2026-09-01, Uberon 2026-10-01, CL 2026-06-08:

| | Uberon | CL |
|---|---|---|
| HPO terms with a direct link | 5,759 (28.9%) | 226 (1.1%) |
| with propagation | 8,834 (44.4%) | 488 (2.5%) |
| SSSOM mappings | 9,102 | 478 |

## Repository structure

```
src/                     pipeline scripts
data/outputs/            results, one folder per run
notebooks/hra_hpo_ibd/   maps gene-specific HPO term lists (XIAP, CTLA, FOXB3) onto the links
```

## Limitations

- Propagation uses the nearest matching ancestor, which can be broader than the phenotype (e.g. *artery* for *coronary artery spasm*).
- The review of propagated links was an automated plausibility check, not expert curation.
- Obsolete HPO terms are skipped; results depend on the HPO release used.

## License

The code is released under the [MIT License](LICENSE). The mapping files are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

The mappings are derived from the [Human Phenotype Ontology](https://hpo.jax.org) (HPO 2026-09-01), [Uberon](http://obophenotype.github.io/uberon/) and the [Cell Ontology](https://obophenotype.github.io/cell-ontology/). If you use them, please also cite these resources and follow the [HPO license](https://hpo.jax.org/app/license).