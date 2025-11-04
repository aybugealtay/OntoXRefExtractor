#!/bin/bash

# URLs for the large files
UBERON_URL="https://purl.obolibrary.org/obo/uberon.owl"
CL_URL="https://purl.obolibrary.org/obo/cl.owl"
HP_URL="https://purl.obolibrary.org/obo/hp.owl"


# Download the files
wget -O data/uberon.owl $UBERON_URL
wget -O data/cl.owl $CL_URL
wget -O data/hp.owl $HP_URL

echo "All files downloaded successfully."