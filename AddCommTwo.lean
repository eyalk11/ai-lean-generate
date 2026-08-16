import Simple

-- For every natural number n, n + 2 = 2 + n.
-- Follows directly from commutativity of natural-number addition.
theorem add_comm_two (n : Nat) : n + 2 = 2 + n :=
  Nat.add_comm n 2
