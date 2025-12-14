export function mapGrades(grades) {
return grades.map(g => ({
subject: g.subject,
value: g.grade,
weight: g.weight,
date: g.date
}));
}