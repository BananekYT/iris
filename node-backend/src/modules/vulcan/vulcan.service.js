const BASE = "https://uonetplus.vulcan.net.pl/{symbol}/LoginEndpoint.aspx";


export async function login({ login, password, symbol }) {
const res = await fetch(`${BASE}/login`, {
method: "POST",
headers: { "Content-Type": "application/json" },
body: JSON.stringify({ login, password, symbol })
});


if (!res.ok) throw new Error("Vulcan: błędne dane logowania");
return res.json();
}


export async function getGrades(token) {
const res = await fetch(`${BASE}/grades`, {
headers: { Authorization: token }
});


if (!res.ok) throw new Error("Vulcan: brak dostępu do ocen");
return res.json();
}