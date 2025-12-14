import Librus from "librus-api";


export async function login({ login, password }) {
const client = new Librus();
await client.authorize(login, password);
return client;
}


export async function getGrades(client) {
return client.getGrades();
}