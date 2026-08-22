import json
import re

with open("C:/Users/Admin/.gemini/antigravity/scratch/TwamProject/docs/2_Backend_and_Database_Reference.md", "r", encoding="utf-8") as f:
    content = f.read()

print("Sections containing 'Part':")
for line in content.split("\n"):
    if line.startswith("# Part"):
        print(line)

print("Found 'Table:' ?", "Table:" in content)
