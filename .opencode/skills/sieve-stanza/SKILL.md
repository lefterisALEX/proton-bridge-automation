---
name: sieve-stanza
description: Generate a Sieve label rule (stanza) to add to sieve/labels.sieve. Use when the user gives a sender address or domain and wants the copy-pasteable rule, e.g. "generate the stanza for foo@example.com label example" or "add a rule for example.com with label news".
---

# Sieve label stanza generator

When the user gives you a sender (exact address, a domain, or both) plus a
label name, produce the exact Sieve `if ... { fileinto ...; stop; }` stanza to
append to `sieve/labels.sieve`. Output only the stanza in a code block so it
can be copy-pasted directly into the Proton web app.

## Conventions used in this repo

- Labels match on `from`, unless the user says otherwise (e.g. a recipient
  alias → use `["to", "cc"]`).
- Duck.com rewrites the `From` header to the alias address (domain becomes
  `duck.com`), so domain rules MUST ALSO match `Duck-Original-From` to catch
  forwarded mail.
- `:domain` only works on `address`/`envelope` tests, NOT on `header`.
  For `Duck-Original-From` use `header :matches` with `*@domain` patterns.
- Keep the label name exactly as the user provides it; the Proton label must
  exist (Proton auto-creates labels referenced by `fileinto`).
- Comment header above each rule, e.g. `# <Label> — <sender>`.

## Output templates

Exact address:

```sieve
# <Label> — <sender>
if address :is "from" "<sender>" {
    fileinto "<label>";
    stop;
}
```

Domain (always include the Duck-Original-From match):

```sieve
# <Label> — <domain>
if anyof (
    address :domain :matches "from" ["<domain>", "*.<domain>"],
    header :matches "Duck-Original-From" ["*@<domain>", "*@*.<domain>"]
) {
    fileinto "<label>";
    stop;
}
```

Multiple senders for one label → combine into a single `anyof (...)` block,
one entry per sender (exact addresses via `address :is "from"` lines, domains
as shown above).

## Input handling

- If the user gives only a domain, produce the domain template and ask what
  label to use if not given (suggest the domain or a short name).
- If the user gives an exact `name@example.com`, produce the exact-address
  stanza. If they also mention wanting "any mail from" that domain, produce
  the domain template instead.
- If the user gives a duck.com alias address (contains `_at_`), you can offer
  the `Duck-Original-From` domain match on the embedded original domain.
- Ask for the label only when it is genuinely ambiguous; otherwise default to
  a sensible name.

## After outputting

Remind the user to paste the rule into the Proton web app (Settings -> Filters
-> replace the Sieve source -> Save) and, if they also want it stored, offer to
append the stanza to `sieve/labels.sieve`.