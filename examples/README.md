# Java 8 migration fixtures

These are deliberately outdated but buildable applications. The repository has
two independent build roots so batch build discovery exercises both Maven and
Gradle. Old APIs, JUnit 4, Java 8 compiler settings, old dependency/plugin
versions, redundant code, an old container base, and stale operational notes are
intentional.

OpenRewrite can safely automate many source/build changes. Files such as the old
runbook and `gradle-old.properties` are included to demonstrate an important
edge case: arbitrary organization-specific prose and dead files must be reviewed
and should not be deleted merely because their names look old.
