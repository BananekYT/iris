import * as service from "./vulcan.service.js";
import { mapGrades } from "./vulcan.mapper.js";


export async function login(req, res) {
try {
const data = await service.login(req.body);
res.json(data);
} catch (e) {
res.status(401).json({ error: e.message });
}
}


export async function grades(req, res) {
try {
const raw = await service.getGrades(req.headers.authorization);
res.json(mapGrades(raw));
} catch (e) {
res.status(401).json({ error: e.message });
}
}