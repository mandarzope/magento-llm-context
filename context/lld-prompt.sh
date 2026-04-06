#!/bin/bash

# Check if a path was provided
if [ -z "$1" ]; then
    echo "Usage: ./generate_lld_prompt.sh app/code/Vendor/Module"
    exit 1
fi

MODULE_PATH=$1
MODULE_NAME=$(echo "$MODULE_PATH" | awk -F'/' '{print $(NF-1)"_"$NF}')

if [ ! -d "$MODULE_PATH" ]; then
    echo "Error: Directory $MODULE_PATH does not exist."
    exit 1
fi

echo "Gathering code for $MODULE_NAME (Strict Bash Compatibility)..."

# Start the Prompt
cat << EOF > lld_prompt.txt
# ROLE
Act as a Magento 2 Lead Architect and Senior Developer.

# TASK
Generate a comprehensive Low-Level Design (LLD) document based on the existing source code provided below for the module: $MODULE_NAME.

# TARGET MODULE CODEBASE
Below is the source code. Inline // comments containing code-like characters have been stripped to focus on active logic, while all block comments have been preserved.

EOF

# Loop through files
find "$MODULE_PATH" -type f \( -name "*.php" -o -name "*.xml" \) | while read -r file; do
    echo "--- FILE: $file ---" >> lld_prompt.txt
    
    # Extract extension in a POSIX-compliant way
    ext="${file##*.}"
    echo '```'"$ext" >> lld_prompt.txt
    
    case "$file" in
        *.php)
            # PERL LOGIC: Strips // comments that look like code ($ ; { })
            perl -pe 's/\/\/.*[;\{\}\$].*//g' "$file" >> lld_prompt.txt
            ;;
        *)
            # For XML and other files
            cat "$file" >> lld_prompt.txt
            ;;
    esac
    
    echo -e "\n" >> lld_prompt.txt
    echo '```' >> lld_prompt.txt
    echo -e "\n\n" >> lld_prompt.txt
done

# Append the LLD Requirements
cat << 'EOF' >> lld_prompt.txt

# LLD GENERATION REQUIREMENTS
Based on the code provided above, generate a detailed Low-Level Design. For every class found or required, provide:

1. **Class Type & Path:** Categorize by Magento folder type (e.g., Api/Data, Model/ResourceModel, Service, Plugin, Observer, Controller, ViewModel).
2. **Interface Strategy:** State if it implements a Service Contract (Interface) or is a concrete implementation.
3. **Entry Point Classification:** Identify if the logic is triggered by a Controller, WebAPI, Cron, GraphQL, or Utility.
4. **Dependency Analysis:** List all classes injected via \`__construct\`. Explain the architectural reason for each.
5. **Method Roadmap & Business Logic:** - List methods with full signatures.
   - Describe the step-by-step business logic for each function.
6. **Interaction Type:** Specify if the function is interfaced by a Controller, WebAPI, Cron, or GraphQL resolver.

# ARCHITECTURAL EVALUATION
- Suggest if logic should be moved to a Service Layer/Repository.
- Evaluate Plugins vs Observers for the existing hooks.
- Identify potential caching impacts (Identities/Tags).

# OUTPUT FORMAT
Organize the output by Domain Logic Groupings. Use Markdown tables for method summaries.
EOF

echo "Done! Prompt generated in: lld_prompt.txt"