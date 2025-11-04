# HPO Preprocessing Project

This project extracts anatomical (UBERON) and cellular (CL) cross-references from the Human Phenotype Ontology (HPO).
## Setup

```bash
git clone https://github.com/aybugealtay/OntoXRefExtractor.git
cd OntoXRefExtractor
python -m venv hpo_env
source hpo_env/bin/activate   # macOS/Linux

pip install -r requirements.txt
