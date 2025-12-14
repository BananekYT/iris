export function mapGrades(data) {
return data.map(g => ({
subject: g.Subject?.Name,
value: g.Content,
weight: g.Weight,
date: g.DateCreated
}));
}