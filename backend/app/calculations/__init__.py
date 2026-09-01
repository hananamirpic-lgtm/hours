"""Pure calculation modules.

Nothing in this package may import the database, the HTTP layer or read the clock. Every function
takes plain values and returns plain values, which is what makes the hour, payroll and billing logic
testable in isolation and reviewable against the worked examples in the requirements.
"""
