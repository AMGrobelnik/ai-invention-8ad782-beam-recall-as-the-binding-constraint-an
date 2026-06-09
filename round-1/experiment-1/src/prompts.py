"""All prompt templates for the FOLIO heterogeneous-oracle pipeline."""

FEW_SHOT_FOL_GENERATION = '''Translate each English sentence into first-order logic (FOL). Use standard syntax: forall X., exists X., ->, &, |, not.

Example 1:
Sentences: "All cats are mammals. Whiskers is a cat."
Conclusion: "Whiskers is a mammal."
FOL translations:
  Premise 1: forall X. (Cat(X) -> Mammal(X))
  Premise 2: Cat(whiskers)
  Conclusion: Mammal(whiskers)

Example 2:
Sentences: "Some students passed every exam. Alex is a student."
Conclusion: "Alex passed some exam."
FOL translations:
  Premise 1: exists X. (Student(X) & forall Y. (Exam(Y) -> Passed(X, Y)))
  Premise 2: Student(alex)
  Conclusion: exists Y. (Exam(Y) & Passed(alex, Y))

Example 3:
Sentences: "No bird can fly if it is a penguin. Tweety is a bird. Tweety is not a penguin."
Conclusion: "Tweety can fly."
FOL translations:
  Premise 1: forall X. (Bird(X) & Penguin(X) -> not CanFly(X))
  Premise 2: Bird(tweety)
  Premise 3: not Penguin(tweety)
  Conclusion: CanFly(tweety)

Now translate:
Sentences: {premises}
Conclusion: {conclusion}
FOL translations:
'''

DIAGNOSTIC_WORLD_GENERATION = '''You are helping evaluate logical translations. Given an English sentence and candidate FOL translations, generate a small world that DISTINGUISHES between different interpretations.

Sentence: {sentence}
Candidate FOL translations (may differ in quantifier scope or negation):
{candidates}

Generate a world with 3-5 named individuals where at least two candidates disagree on the truth value of the sentence. Use proper names (alice, bob, carol, etc.) not variables.

Output ONLY valid JSON in this exact format:
{{"domain": ["alice", "bob", "carol"], "atoms": {{"Predicate(alice)": true, "Predicate(bob)": false, "Relation(alice,bob)": true}}}}

World:
'''

ORACLE_TRUTH_JUDGMENT = '''Given a world description and an English sentence, decide if the sentence is TRUE or FALSE in that world.

World:
- Individuals: {domain}
- Facts: {atoms}

Sentence: "{sentence}"

Is this sentence true in this world? Think briefly, then answer with exactly one word: yes or no.

Answer:'''

DIRECT_LLM_JUDGE = '''Here are {k} FOL translations of the English sentence. Select the one that best captures the meaning.

English: "{sentence}"

Candidates:
{candidates_numbered}

Which candidate (1-{k}) is most semantically faithful? Answer with just the number.

Answer:'''
