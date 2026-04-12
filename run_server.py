import os
import sys
import runpy

os.chdir(r"C:\Tools\ews-mcp")
sys.path.insert(0, r"C:\Tools\ews-mcp")
runpy.run_module("src.main", run_name="__main__")