import os
import zipfile
import fnmatch
from datetime import datetime

def load_gitignore_patterns(base_dir):
    patterns = [
        '.git', '.git/*', '.claude', '.claude/*', '__pycache__', '__pycache__/*', 
        '*.zip', '.env', 'logs', 'logs/*', '*.log', '*.py[cod]', '*.swp',
        '.venv', '.venv/*', 'venv', 'venv/*', 'env', 'env/*'
    ]
    gitignore_path = os.path.join(base_dir, '.gitignore')
    if os.path.exists(gitignore_path):
        with open(gitignore_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Add to patterns
                patterns.append(line)
                # Also handle directory matching
                if line.endswith('/'):
                    patterns.append(line[:-1])
                    patterns.append(line + '*')
    return list(set(patterns))

def should_exclude(rel_path, patterns):
    # Standardize path separators to forward slashes for pattern matching
    normalized_path = rel_path.replace('\\', '/')
    parts = normalized_path.split('/')
    
    for pattern in patterns:
        normalized_pattern = pattern.replace('\\', '/')
        # Direct match or pattern match
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True
        # Match subdirectories/files inside ignored directories
        if any(fnmatch.fnmatch(part, normalized_pattern) for part in parts):
            return True
            
    return False

def zip_project():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    date_str = datetime.now().strftime('%Y%m%d')
    zip_name = f'autostock_backup_{date_str}.zip'
    zip_path = os.path.join(base_dir, zip_name)
    
    patterns = load_gitignore_patterns(base_dir)
    print(f"Loaded exclude patterns: {patterns}")
    
    included_files = []
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(base_dir):
            # Calculate relative path
            rel_dir = os.path.relpath(root, base_dir)
            if rel_dir == '.':
                rel_dir = ''
                
            # Filter directories in-place to prevent os.walk from entering them
            filtered_dirs = []
            for d in dirs:
                dir_rel_path = os.path.join(rel_dir, d) if rel_dir else d
                if not should_exclude(dir_rel_path, patterns):
                    filtered_dirs.append(d)
            dirs[:] = filtered_dirs
            
            for file in files:
                file_rel_path = os.path.join(rel_dir, file) if rel_dir else file
                if not should_exclude(file_rel_path, patterns):
                    full_path = os.path.join(root, file)
                    zip_file.write(full_path, file_rel_path)
                    included_files.append(file_rel_path)
                    
    print(f"\nSuccessfully compressed {len(included_files)} files into {zip_name}!")
    print("\nIncluded files sample:")
    for f in sorted(included_files)[:30]:
        print(f" - {f}")
    if len(included_files) > 30:
        print(f" ... and {len(included_files) - 30} more files.")

if __name__ == '__main__':
    zip_project()
