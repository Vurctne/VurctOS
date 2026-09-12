# Independent design review

A contract template for the review that finds what a normal review cannot.

## The problem this solves

Ask a reviewer to review your design and you get a review of your design. It checks your structure for internal consistency, catches contradictions inside it, and hands back a verdict. What it almost never does is notice the thing you never wrote down, because your document is the frame it reasons in.

Ask the same reviewer to produce its own design from the same inputs, and the gaps appear on their own: wherever the two designs differ, one of you has made a choice the other did not know was a choice.

A worked instance, 2026-09-12: six consecutive review rounds on an architecture document missed that the document contradicted itself about which layer owned request retries. One independent-design pass found it immediately, and also found a decision the brief had never made at all (where a shared ledger physically lives, as opposed to what its primary key is). It further predicted, correctly, which of those omissions an implementer would get wrong first.

## When to use it

Before a load-bearing design is frozen: module boundaries, public interfaces, state machines, concurrency and crash recovery, security or trust boundaries, data layout, wire protocols. Anything whose cost of being wrong is a migration rather than an edit.

Not for prose, not for a bug fix, not for a change whose blast radius is one file.

## When not to bother

If the design has one obvious shape, an independent proposal will restate it and you will have spent a review round confirming what you already knew. The value comes from genuine degrees of freedom.

## How to run it

Give the reviewer the same inputs you gave the implementer: the specification, the constraints, the settled decisions, the existing code it must live beside, and your own brief verbatim. Withholding your brief does not buy independence, it just makes the comparison section impossible; the independence that matters is in the instruction, not in the information.

Then ask for four sections, in this order, and insist on the third:

1. **Its own design.** Concrete enough to implement from: file split with responsibilities, key interface signatures with types, the data flow of one representative command end to end, and the order it would build things in.
2. **Its review of your brief.** Instructions that would produce a bad structure, omissions, over-constraints that bind the implementer to an unnecessary choice, and conflicts with the specification.
3. **The disagreement list.** Every substantive difference between its design, the existing design documents, and your brief. Per item: what the disagreement is, what each way costs, which it recommends, and what the wrong choice costs later. **This is the deliverable.** Everything else is context for it.
4. **The three things most likely to go wrong**, ranked by probability, each with a test that would catch it the same day the code is written.

Ask for interface signatures in code blocks. Ask it not to start implementing.

## Reading the result

Adjudicate every disagreement yourself, in writing, before any of it reaches the implementer. A disagreement you cannot decide is a question for the person whose product it is, not a coin flip.

Where the reviewer is right, say so plainly in the correction you send the implementer, and record what your brief got wrong. The failure mode of this technique is treating the independent design as a menu of suggestions: the items it finds are usually the ones your own frame could not see, which is exactly why they feel optional.

If implementation has already started, send the adjudications immediately rather than waiting for a clean stopping point. Rework is cheapest in the first hour.

## The prompt

`contract.md` in this directory is the contract, with placeholders for the paths and the decisions of your own project.
