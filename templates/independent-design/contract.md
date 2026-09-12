# Contract: independent design for <COMPONENT>

Role: **independent architect, not reviewer.** This is <WHY IT IS LOAD BEARING: what later work depends on it>. What is wanted is **your own design**, not an assessment of whether the existing one is good. Where two independent designs differ, one of us has made a choice the other did not know was a choice, and those are what this pass is for.

## Prohibited

- Do not auto-invoke any skill, plugin or subagent; read-only, change no files, no commits.
- **Do not start writing implementation code.** Designs and interface signatures only.

## Read

- <SPECIFICATION AND ARCHITECTURE DOCUMENTS, with the sections that bind this component>
- <SETTLED DECISIONS: the decision record, and any decision that has already been taken and must not be reopened>
- <EXISTING CODE this must live beside, including anything already proven in production>
- <THE BRIEF GIVEN TO THE IMPLEMENTER, verbatim, at an absolute path>

## Produce

### Section 1: your design

Concrete enough to implement from:

- **File split**: each file's responsibility and order of magnitude in lines. Where this differs from the existing module map, say where and why.
- **Key interface signatures**: at minimum <LIST THE FIVE OR SIX SEAMS THAT MATTER>, with type annotations, plus the direction of calls between them.
- **Data flow**: `<ONE REPRESENTATIVE COMMAND>` from entry point to disk, step by step.
- **<DOMAIN SPECIFIC CONCERNS: persistence and migration, concurrency, authentication, whatever this component actually has to get right>**
- **Testing strategy**: how the external boundary is doubled, what uses real files and what uses temporary ones, how the full flow is covered without network access.
- **Build order**: the sequence you would write these files in, and why.

### Section 2: review of the brief

Point by point: instructions that would produce a bad structure; missing essentials; over-constraints that bind the implementer to an unnecessary choice; conflicts with the specification or the settled decisions. Evidence for each (file and line, or the clause), and a replacement wording.

### Section 3: the disagreement list

Compare your design, the existing design documents and the brief. List the **substantive** disagreements, not wording differences. Per item: the disagreement, what each way costs, which you recommend, and what the wrong choice costs later. **This section is the most important output of this pass.**

### Section 4: the three things most likely to go wrong

Ranked by probability that an implementer following the current brief gets it wrong. Per item: why it is likely, and the test that would catch it the same day the code is written.

## Output

Markdown. Interface signatures in fenced code blocks. Your judgement, not restatements of the documents you were given.
