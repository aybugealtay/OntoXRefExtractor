## Information  about this testing ontology

test_ontology.owl is a custom ontology built to express 
most of the possible relations in order to test Ontolearn's
functionalities easily.

You will find below the Tbox and Abox of this ontology:

### Tbox

```
---------Object Properties---------
Domain(r1) = S ⊓ T, Range(r1) = G
r2 ⊑ r1
r3 ⊑ r4
r7
r5 ⊓ r1 = ∅
r5 ≡ r6

---------Data Properties---------
dp2 ⊑ dp1
dp3 ⊓ dp1 = ∅

---------Classes-----------
AB ≡ (A ⊓ B), AB ⊑ C
D ⊑ (r7.E ⊓ B)
F ≡ r2.G, F ⊑ H
I ⊑ (J ⊓ K)
L ⊓ M = ∅
N ≡ Q
O ⊑ P
R ⊑ r5.Q
(S ⊓ T) ⊑ U
```

### Abox

```
o is O
p is P
a is A ^ B
b is B, b has r1.f
c is I
d is D
e is AB
f is E
g is G
n is N, n has r3.q, r4.l, r6.s, r5.Q
m is M
l is L
l ≠ m
q is Q
ind1 has r5.q, r2.g, r6.(S ⊓ T)
r is R
s is S ^ T
```


