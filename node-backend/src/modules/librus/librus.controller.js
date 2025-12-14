import * as service from "./librus.service.js";
import { mapGrades } from "./librus.mapper.js";


let session;


export async function login(req, res) {
try {
session = await service.login(req.body);
res.json({ success: true });
} catch {
res.status(401).json({ error: "Librus: błąd logowania" });
}
}


export async function grades(req, res) {
try {
const grades = await service.getGrades(session);
res.json(mapGrades(grades));
} catch {
res.status(401).json({ error: "Librus: brak ocen" });
}
}