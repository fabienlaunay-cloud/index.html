# index.html
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>CarbonFlow – Simulation CO₂</title>
<style>
body {
  font-family: Arial, sans-serif;
  background: #f4f7f8;
  display: flex;
  justify-content: center;
  padding: 30px;
}
.container {
  background: #ffffff;
  padding: 25px 30px;
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  width: 400px;
}
h2 {
  text-align: center;
  color: #333;
  margin-bottom: 20px;
}
input, select, button {
  margin-bottom: 15px;
  display: block;
  width: 100%;
  padding: 10px;
  border-radius: 5px;
  border: 1px solid #ccc;
  box-sizing: border-box;
  font-size: 14px;
}
button {
  background: #28a745;
  color: white;
  font-weight: bold;
  border: none;
  cursor: pointer;
  transition: background 0.3s;
}
button:hover {
  background: #218838;
}
#result {
  margin-top: 20px;
  font-weight: bold;
  text-align: center;
}
</style>
</head>
<body>
<div class="container">
<h2>CarbonFlow – Simulation CO₂</h2>

<label>Shipping ID</label>
<input type="text" id="shipment" placeholder="Ex: SH001" required>

<label>Pays de départ</label>
<input type="text" id="pays_depart" placeholder="Ex: Chine" required>

<label>Pays d'arrivée</label>
<input type="text" id="pays_arrivee" placeholder="Ex: Belgique" required>

<label>Distance (km)</label>
<input type="number" id="distance" placeholder="Nombre de km" required>

<label>Poids (kg)</label>
<input type="number" id="poids" placeholder="Poids en kg" required>

<label>Mode de transport</label>
<select id="transport">
  <option value="camion">Camion</option>
  <option value="avion">Avion</option>
  <option value="bateau">Bateau</option>
  <option value="train">Train</option>
</select>

<label>Date</label>
<input type="date" id="date" required>

<label>Email</label>
<input type="email" id="email" placeholder="votre@email.com" required>

<button onclick="calculerCO2()">Calculer CO₂</button>

<div id="result"></div>
</div>

<script>
function calculerCO2() {
  let shipment = document.getElementById("shipment").value;
  let pays_depart = document.getElementById("pays_depart").value;
  let pays_arrivee = document.getElementById("pays_arrivee").value;
  let distance = parseFloat(document.getElementById("distance").value);
  let poids = parseFloat(document.getElementById("poids").value);
  let transport = document.getElementById("transport").value;
  let date = document.getElementById("date").value;
  let email = document.getElementById("email").value;

  // Facteurs d'émission (kg CO₂ / t.km)
  const facteurs = { camion:0.1, avion:0.6, bateau:0.015, train:0.01 };

  // Calcul CO2
  let co2 = distance * (poids/1000) * facteurs[transport];

  // Calcul score A-E
  let score;
  if(co2 < 5) score = 'A';
  else if(co2 < 15) score = 'B';
  else if(co2 < 30) score = 'C';
  else if(co2 < 60) score = 'D';
  else score = 'E';

  // Affichage
  document.getElementById("result").innerHTML = `
    <p><strong>Shipment ID :</strong> ${shipment}</p>
    <p><strong>Trajet :</strong> ${pays_depart} → ${pays_arrivee}</p>
    <p><strong>Distance :</strong> ${distance} km</p>
    <p><strong>Poids :</strong> ${poids} kg</p>
    <p><strong>Mode :</strong> ${transport}</p>
    <p><strong>CO₂ estimé :</strong> ${co2.toFixed(2)} kg</p>
    <p><strong>Score :</strong> ${score}</p>
  `;
}
</script>
</body>
</html>
