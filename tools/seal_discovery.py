#!/usr/bin/env python3
"""Seal already-collected bytes after original latest-file generation."""
import json
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.discovery_archive import seal
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection-status',default='success',choices=['success','failure','cancelled'])
    args=parser.parse_args()
    print(json.dumps(seal(args.collection_status),ensure_ascii=False))
