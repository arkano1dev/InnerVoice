#!/usr/bin/env python3
"""Exercise the provider with a private fixture; print content-free measurements."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import re

def word_error_rate(expected,actual):
    reference=re.findall(r"\w+",expected.casefold())
    hypothesis=re.findall(r"\w+",actual.casefold())
    previous=list(range(len(hypothesis)+1))
    for index,word in enumerate(reference,1):
        current=[index]
        for j,token in enumerate(hypothesis,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(word!=token)))
        previous=current
    return previous[-1]/len(reference) if reference else None


parser=argparse.ArgumentParser()
parser.add_argument("config",type=Path)
parser.add_argument("audio",type=Path)
parser.add_argument("reference",type=Path)
parser.add_argument("--repeats",type=int,default=1)
args=parser.parse_args()
duration=float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration",
                                       "-of","default=nw=1:nk=1",args.audio]))
reference=" ".join(args.reference.read_text().casefold().split())
failed=False
with tempfile.TemporaryDirectory() as work:
    for i in range(args.repeats):
        start=time.monotonic()
        result=Path(work)/f"{i}.txt"
        subprocess.run([sys.executable,str(Path(__file__).with_name("provider.py")),"--config",args.config,args.audio,result],
                       env=dict(os.environ,INNERVOICE_EVENT_ID=uuid.uuid4().hex),check=True)
        elapsed=time.monotonic()-start
        actual=result.read_text()
        passed=" ".join(actual.casefold().split())==reference
        failed|=not passed
        print(json.dumps(dict(run=i+1,seconds=round(elapsed,3),rtf=round(elapsed/duration,3),reference_match=passed,word_error_rate=word_error_rate(reference,actual))))
sys.exit(int(failed))
