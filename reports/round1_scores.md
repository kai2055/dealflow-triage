# round1 scores

Plan `bakeoff/round1.toml`, instructions `pitch_card_v2`, scored 2026-10-19 22:23 UTC. Answer sheets drafted by Kimi, every field checked by hand (decision 80).

| Model | Runs ok / failed | Invented | Wrong | Missed | Correct | Correctly empty | Valid extra | Unjudged | Hand-judged | Cost | Output mode |
|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic/claude-sonnet-5.5 | 3 / 3 | 2 | 1 | 5 | 27 | 9 | 4 | 0 | 12 | $0.1517 | json, strict |
| openai/gpt-6.1-sol | 3 / 0 | 3 | 1 | 3 | 29 | 8 | 5 | 0 | 11 | $0.1533 | strict |
| google/gemini-4-argon | 3 / 0 | 3 | 1 | 2 | 30 | 8 | 4 | 0 | 13 | $0.1092 | strict |

Counts cover the 9 single fields plus every team member and traction claim. Wrong includes a traction claim filed under the wrong kind; a different team role is listed below but still counts the person as correct (decision 88). Output mode: strict means the provider enforced the card layout; json means the layout was only in the instructions and the card was checked in code afterwards (decision 93).

| Model | Quotes verified | Not in text | Unverifiable | Bad |
|---|---|---|---|---|
| anthropic/claude-sonnet-5.5 | 12 | 2 | 20 | 0 |
| openai/gpt-6.1-sol | 12 | 2 | 24 | 0 |
| google/gemini-4-argon | 11 | 3 | 24 | 0 |

## Every outcome other than correct

### anthropic/claude-sonnet-5.5 on DecisiveHealth (attempt 10)

- failed: model_rejected

### openai/gpt-6.1-sol on DecisiveHealth (attempt 13)

- invented: sector = MEDICAL BILLING (quote page 1)

### google/gemini-4-argon on DecisiveHealth (attempt 16)

- invented: sector = Urgent Care Market (quote page 8)

### anthropic/claude-sonnet-5.5 on DecisiveHealth (attempt 19)

- missed: one_liner = Clear Medical Billing
- invented: sector = medical billing (quote page 1)

### anthropic/claude-sonnet-5.5 on Cyclica (attempt 11)

- failed: model_rejected

### openai/gpt-6.1-sol on Cyclica (attempt 14)

- wrong kind: traction 'working with many top 50 pharma, biotech, and academia': sheet customers / model other
- valid extra: traction item 'revenue: Continuing to work with global pharma cos. through revenue generating engagements.' (quote page 2)
- valid extra: traction item 'other: Designed advanced leads that minimize off-target activity while ensuring drug-like and ADMET properties.' (quote page 2)
- valid extra: traction item 'other: Business model adopted in the industry, contributing to research efforts of early stage biotech, research institutes, and multinational companies globally.' (quote page 7)
- valid extra: traction item 'other: Operational since 2014.' (quote page 8)
- valid extra: traction item 'other: 35 employees; computational scientists, experimentalists, data scientists, software developers.' (quote page 8)
- missed: traction item 'partnership: Tieös Pharmaceuticals collaboration'
- missed: traction item 'partnership: Merck KGaA Darmstadt, Germany'

### google/gemini-4-argon on Cyclica (attempt 17)

- valid extra: traction item 'revenue: Continuing to work with global pharma cos. through revenue generating engagements' (quote page 2)
- valid extra: traction item 'customers: Business model adopted by early stage biotech, research institutes, and multinational companies globally, including Tieös Pharmaceuticals, Merck KGaA, and University of Toronto' (quote page 7)
- valid extra: traction item 'other: Operational since 2014' (quote page 8)
- valid extra: traction item 'other: 35 employees' (quote page 8)
- missed: traction item 'partnership: Tieös Pharmaceuticals collaboration'
- missed: traction item 'partnership: Merck KGaA Darmstadt, Germany'

### anthropic/claude-sonnet-5.5 on Cyclica (attempt 20)

- wrong: sector: sheet 'neo biotechnology' / model 'AI in Drug Discovery'
- valid extra: traction item 'revenue: Continuing to work with global pharma cos. through revenue generating engagements' (quote page 2)
- valid extra: traction item 'other: Validated and commercialized (2015 - ongoing)' (quote page 8)
- valid extra: traction item 'other: 35 employees' (quote page 8)
- valid extra: traction item 'other: Goal to create over 300 programs by 2025 through partnership model' (quote page 2)
- missed: traction item 'partnership: Tieös Pharmaceuticals collaboration'
- missed: traction item 'partnership: Merck KGaA Darmstadt, Germany'
- missed: traction item 'other: Technology developed and patented at Cyclica (2014 - ongoing); strong IP position'

### anthropic/claude-sonnet-5.5 on Gridcure (attempt 12)

- failed: model_rejected

### openai/gpt-6.1-sol on Gridcure (attempt 15)

- invented: sector = smart grid (quote page 3)
- invented: product = POWERFUL ANALYTICS FOR GRID SECURITY (quote page 1)
- role differs: Kim Montgomery: sheet 'Ph. D Applied Mathematics' / model None
- missed: traction item 'partnership: Burns McDonnell'

### google/gemini-4-argon on Gridcure (attempt 18)

- invented: sector = smart grid (quote page 3)
- invented: product = POWERFUL ANALYTICS FOR GRID SECURITY (quote page 1)
- role differs: Kim Montgomery: sheet 'Ph. D Applied Mathematics' / model None
- wrong kind: traction 'Burns McDonnell': sheet partnership / model other

### anthropic/claude-sonnet-5.5 on Gridcure (attempt 21)

- invented: product = Powerful analytics for grid security (quote page 1)
- role differs: Kim Montgomery: sheet 'Ph. D Applied Mathematics' / model None
- missed: traction item 'partnership: Burns McDonnell'

