"""The closed list of places in the repository that name a container tool or the container machine: every
function, class or top of a tracked Python, shell or workflow file in which such a name appears, with the reason it
is there. A new place fails the lock until it is entered here with its reason."""

PLACES: dict[tuple[str, str], str] = {}
