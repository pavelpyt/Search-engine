---
name: question-decomposition
description: Turn a vague question into the goal behind it and 2-5 searchable sub-questions, the way a consultant asks before answering - what is the decision, what can be measured, what is missing. Use when routing a new question.
version: "1.0"
---

# Question decomposition

Before searching, understand what the user needs. Ask the questions a good consultant asks
first, then turn the answers into sub-questions that can each be searched on their own.

## Step 1 – find the goal
Ask yourself (do not ask the user):
- **Decision:** what will the user decide or do with the answer? (buy, build, compare, explain)
- **Scope:** which place, period and audience? If not stated, assume the most likely
  (the user's language suggests the country) and write the assumption into `goal`.
- **Measure:** what number or fact would settle it? (price, share, date, count, ranking)

## Step 2 – split into sub-questions
Cover, when relevant:
1. **Current state** – what is true now (with dates).
2. **Numbers** – the figures that matter for the decision.
3. **Options or comparison** – the alternatives the user is choosing between.
4. **Risks and costs** – what can go wrong, what it costs.
5. **Open points** – what is disputed or changing.

Each sub-question must be searchable alone (contain the subject, not "it"), specific, and
answerable with sources. Prefer 3; never more than 5.

## Step 3 – rate complexity
- `simple`: one fact, stable, little ambiguity.
- `moderate`: several sources or numbers, or recent changes.
- `complex`: conflicting evidence, expert judgement, legal/medical/financial stakes, or
  multi-step reasoning across domains.
When unsure between two levels, pick the higher one – a too-weak model costs more than a
slightly stronger one.

## Example
Question: "Vyplatí se malé firmě v Česku nasadit AI chatbota na zákaznickou podporu?"
- goal: decide whether a small Czech company should deploy an AI support chatbot (assume 2026, CZ SMB)
- subquestions:
  1. What do AI customer-support chatbots cost for small businesses in 2026?
  2. What share of support tickets can AI chatbots resolve without a human?
  3. What are the risks of AI chatbots in customer support (GDPR, wrong answers)?
- complexity: moderate
