"""Inspect source artifacts without reading or changing Git configuration."""
from pathlib import Path
import re
import os
import sys

ROOT=Path(__file__).resolve().parents[1]
blocked_dirs={'data','native-state','logs','.venv','node_modules','.runtime','__pycache__','.git'}
blocked_suffixes={'.exe','.sqlite3','.db','.log','.lnk','.pyc','.zip'}
def main():
    paths=[]
    excluded=blocked_dirs|{'.ruff_cache','.pytest_cache','.migration-backups'}
    for directory,folders,files in os.walk(ROOT):
        folders[:]=[name for name in folders if name not in excluded]
        for name in files:
            if name!='local.config.json':paths.append((Path(directory)/name).relative_to(ROOT))
    errors=[]
    for relative in paths:
        if blocked_dirs.intersection(relative.parts) or relative.suffix in blocked_suffixes or relative.name=='local.config.json':
            errors.append(str(relative))
        if relative.suffix in {'.py','.js','.html','.json','.md','.txt','.toml','.yml','.yaml'}:
            text=(ROOT/relative).read_text(encoding='utf-8-sig',errors='replace')
            if re.search(r'\bsk-[A-Za-z0-9]{20,}',text):errors.append(str(relative)+' (possible API key)')
    if errors:
        print('Refusing runtime/private artifacts: '+', '.join(errors));return 1
    print(f'Checked {len(paths)} source files without Git operations: no release binary or apparent API key.')
    return 0
if __name__=='__main__':sys.exit(main())
