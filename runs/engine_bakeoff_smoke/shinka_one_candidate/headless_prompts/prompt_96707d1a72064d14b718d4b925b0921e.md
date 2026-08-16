# System Instructions

Improve a deterministic graph-coloring solver. The candidate must always return a valid coloring for every supplied graph and repeated calls with identical inputs must return identical results. Minimize the number of distinct colors; combined_score is the mean relative improvement over the frozen degree-ordered greedy baseline. Modify only the EVOLVE block.
Redesign the program with a different structural approach while potentially using similar core concepts.
Focus on changing the overall architecture, data flow, or program organization.
You MUST respond using a short summary name, description and the full code:

<NAME>
A shortened name summarizing the code you are proposing. Lowercase, no spaces, underscores allowed.
</NAME>

<DESCRIPTION>
Describe the structural changes you are making and how they improve the program's performance, maintainability, or efficiency.
</DESCRIPTION>

<CODE>
```{language}
# The structurally redesigned program here.
```
</CODE>

* Keep the markers "EVOLVE-BLOCK-START" and "EVOLVE-BLOCK-END" in the code.
* Focus on changing the program's structure: modularization, data flow, control flow, or architectural patterns.
* The core problem-solving approach may be similar but organized differently.
* Ensure the same inputs and outputs are maintained.
* Use the <NAME>, <DESCRIPTION>, and <CODE> delimiters to structure your response. It will be parsed afterwards.

# Previous Messages

[]

# User Request


# Current program

Here is the current program we are trying to improve (you will need to propose a new program with the same inputs and outputs as the original program, but with improved internal implementation):

```python
from __future__ import annotations


# EVOLVE-BLOCK-START
def solve(
    node_count: int,
    edges: tuple[tuple[int, int], ...],
    seed: int,
) -> list[int]:
    """Return a deterministic valid coloring while using as few colors as possible."""
    del seed
    neighbors = [set() for _ in range(node_count)]
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    order = sorted(range(node_count), key=lambda node: (-len(neighbors[node]), node))
    colors = [-1] * node_count
    for node in order:
        blocked = {colors[other] for other in neighbors[node] if colors[other] >= 0}
        color = 0
        while color in blocked:
            color += 1
        colors[node] = color
    return colors
# EVOLVE-BLOCK-END

```

Here are the performance metrics of the program:

Combined score to maximize: 0.00
mean_color_count: 6.75; mean_relative_improvement: 0.00; reproducibility_rate: 1.00; valid_rate: 1.00

# Task

Rewrite the program to improve its performance on the specified metrics.
Provide the complete new program code.

IMPORTANT: Make sure your rewritten program maintains the same inputs and outputs as the original program, but with improved internal implementation.
