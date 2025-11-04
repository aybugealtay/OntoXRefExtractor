# HPO Preprocessing Project

This project extracts anatomical (UBERON) and cellular (CL) cross-references from the Human Phenotype Ontology (HPO) and the Cell Ontology (CL).

## Setup

```bash
git clone <your-repo-url>
cd hpo-preprocess
python -m venv hpo_env
source hpo_env/bin/activate   # macOS/Linux

pip install -r requirements.txt
