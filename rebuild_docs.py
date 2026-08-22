import json
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from database import Base
import main # To ensure all models are registered with Base

doc_path = "C:/Users/Admin/.gemini/antigravity/scratch/TwamProject/docs/2_Backend_and_Database_Reference.md"

def build_api_docs():
    with open("openapi.json", "r", encoding="utf-8") as f:
        openapi = json.load(f)
    
    lines = ["# Backend and Database Reference\n", "# Part 1: API Endpoints\n"]
    
    # Group by tags
    modules = {}
    paths = openapi.get("paths", {})
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            tags = operation.get("tags", ["Default"])
            for tag in tags:
                if tag not in modules:
                    modules[tag] = []
                modules[tag].append((method.upper(), path, operation))
                
    for tag in sorted(modules.keys()):
        lines.append(f"## Module: {tag}\n")
        for method, path, operation in modules[tag]:
            lines.append(f"### `[{method}] {path}`")
            if "summary" in operation:
                lines.append(f"**Summary**: {operation['summary']}")
            if "description" in operation:
                lines.append(f"**Description**: {operation['description']}")
            lines.append("")
            
            # Parameters
            params = operation.get("parameters", [])
            if params:
                lines.append("**Parameters**:")
                lines.append("| Name | In | Type | Required | Description |")
                lines.append("|---|---|---|---|---|")
                for p in params:
                    name = p.get("name", "")
                    in_ = p.get("in", "")
                    req = "Yes" if p.get("required") else "No"
                    schema = p.get("schema", {})
                    p_type = schema.get("type", "any") if schema else "any"
                    desc = p.get("description", "").replace("\n", " ")
                    lines.append(f"| {name} | {in_} | {p_type} | {req} | {desc} |")
                lines.append("")
                
            # Request Body
            req_body = operation.get("requestBody", {})
            if req_body:
                content = req_body.get("content", {})
                if content:
                    lines.append("**Payload / Request Body**:")
                    for c_type, c_data in content.items():
                        schema_ref = c_data.get("schema", {})
                        if "$ref" in schema_ref:
                            schema_name = schema_ref["$ref"].split("/")[-1]
                        else:
                            schema_name = schema_ref.get("type", "object")
                        lines.append(f"- Content-Type: `{c_type}`, Schema: `{schema_name}`")
                lines.append("")
                
            # Responses
            responses = operation.get("responses", {})
            if responses:
                lines.append("**Responses**:")
                for status, resp in responses.items():
                    desc = resp.get("description", "")
                    schema_name = "None"
                    content = resp.get("content", {})
                    if content:
                        for c_type, c_data in content.items():
                            schema_ref = c_data.get("schema", {})
                            if "$ref" in schema_ref:
                                schema_name = schema_ref["$ref"].split("/")[-1]
                            elif schema_ref.get("type") == "array":
                                items = schema_ref.get("items", {})
                                if "$ref" in items:
                                    schema_name = f"array of {items['$ref'].split('/')[-1]}"
                                else:
                                    schema_name = "array"
                            else:
                                schema_name = schema_ref.get("type", "object")
                            break
                    lines.append(f"- `{status}`: {desc} (Schema: `{schema_name}`)")
            lines.append("\n")
            
    return "\n".join(lines)

def build_db_docs():
    lines = ["# Part 2: Database Schema (SQLAlchemy Models)\n"]
    for table_name, table in Base.metadata.tables.items():
        lines.append(f"## Table: {table_name}")
        if table.schema:
            lines.append(f"**Schema:** {table.schema}\n")
        else:
            lines.append("")
            
        lines.append("### Columns")
        lines.append("| Name | Type | Primary Key | Nullable | Default | Foreign Keys |")
        lines.append("|------|------|-------------|----------|---------|--------------|")
        for col in table.columns:
            fks = ", ".join([fk.target_fullname for fk in col.foreign_keys])
            default = str(col.default.arg) if col.default else ""
            pk = "Yes" if col.primary_key else "No"
            null = "Yes" if col.nullable else "No"
            lines.append(f"| {col.name} | {col.type} | {pk} | {null} | {default} | {fks} |")
        
        lines.append("\n")
        
    return "\n".join(lines)

if __name__ == "__main__":
    api_docs = build_api_docs()
    db_docs = build_db_docs()
    
    final_content = api_docs + "\n" + db_docs
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print("Documentation fully regenerated and updated successfully!")
