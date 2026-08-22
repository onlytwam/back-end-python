import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    import main
    from database import Base
    
    with open('C:\\Users\\Admin\\.gemini\\antigravity\\scratch\\TwamProject\\docs\\5_Database_Schema.md', 'w') as f:
        f.write('# Database Schema Documentation\n\n')
        
        for table_name, table in Base.metadata.tables.items():
            f.write(f'## Table: {table_name}\n')
            if table.schema:
                f.write(f'**Schema:** {table.schema}\n\n')
            
            f.write('### Columns\n')
            f.write('| Name | Type | Primary Key | Nullable | Default | Foreign Keys |\n')
            f.write('|------|------|-------------|----------|---------|--------------|\n')
            for col in table.columns:
                fks = ', '.join([fk.target_fullname for fk in col.foreign_keys])
                default = str(col.default.arg) if col.default else ''
                f.write(f'| {col.name} | {col.type} | {col.primary_key} | {col.nullable} | {default} | {fks} |\n')
            
            f.write('\n\n')
            
    print("Documentation generated successfully.")
except Exception as e:
    import traceback
    traceback.print_exc()
