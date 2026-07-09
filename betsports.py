import os
import re
from datetime import datetime, timezone
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecreto123'

# 2. Configuração do Banco
db_url = os.environ.get('DATABASE_URL', 'postgresql://neondb_owner:npg_meVRBsiA3D2U@ep-gentle-salad-adp0i6vu-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['GLOBAL_AUTO_REFRESH'] = False

# 3. Instanciar Extensões (apenas uma vez)
db = SQLAlchemy(app)
migrate = Migrate(app, db) # Agora o comando 'db' vai funcionar!
login_manager = LoginManager(app)
login_manager.login_view = 'login'

OPCOES_PADRAO = [
    "Casa vence", "Empate", "Fora vence",
    "+ de 0 gol","+ 1 gol", "+ 2 gols", "+ 3 gols", "+ 4 gols", "+ 5 gols",
    "- 1 gol", "- 2 gols", "- 3 gols", "- 4 gols", "- 5 gols",
    "gol de cabeça", "sem gols", "+ 2 cartões", "expulsões","gol de bicicleta",
    "+ 5 escanteios","+ 10 escanteios","- 10 escanteios","- 5 escanteios",
    "gol de penalti","penalti perdido","gol no primeiro tempo","sem gol primeiro tempo",
    "+ 0 gol no primeiro tempo","+ de 1 gol no primeiro tempo","ambos marcam - sim ","ambos marcam - nao"
]

# ================= HELPER FUNCTIONS (MOTOR CENTRAL DE REGRAS) =================

def get_game_metrics(game):
    """Extrai todas as estatísticas da partida em um dicionário unificado."""
    return {
        'home': game.home_score or 0,
        'away': game.away_score or 0,
        'total_goals': (game.home_score or 0) + (game.away_score or 0),
        'total_cards': (game.home_cards or 0) + (game.away_cards or 0),
        'total_headers': (game.home_headers or 0) + (game.away_headers or 0),
        'total_corners': (game.home_corners or 0) + (game.away_corners or 0),
        'total_bicycle': (game.home_bicycle_goals or 0) + (game.away_bicycle_goals or 0),
        'total_penalties_scored': (game.home_penalties_scored or 0) + (game.away_penalties_scored or 0),
        'total_penalties_missed': (game.home_penalties_missed or 0) + (game.away_penalties_missed or 0),
        'total_first_half_goals': (game.home_first_half_goals or 0) + (game.away_first_half_goals or 0),
        'total_expulsions': (game.home_expulsions or 0) + (game.away_expulsions or 0)
    }

def get_metric_value_for_cashout(desc, metrics):
    """Identifica qual métrica numérica a odd está monitorando (usado no Cashout)."""
    desc = desc.lower().strip()
    if "cart" in desc or "card" in desc:
        return metrics['total_cards']
    elif "cabeç" in desc or "header" in desc:
        return metrics['total_headers']
    elif "escanteio" in desc or "corner" in desc:
        return metrics['total_corners']
    elif "primeiro tempo" in desc or "1º tempo" in desc:
        return metrics['total_first_half_goals']
    elif "expuls" in desc:
        return metrics['total_expulsions']
    else:
        return metrics['total_goals']

def evaluate_odd_logic(desc, metrics):
    """Verifica se a odd bateu com base nas métricas atuais."""
    desc = desc.lower().strip()
    
    if "casa vence" in desc:
        return metrics['home'] > metrics['away']
    elif "fora vence" in desc:
        return metrics['away'] > metrics['home']
    elif "empate" in desc:
        return metrics['home'] == metrics['away']
    elif "sem gols" in desc:
        return metrics['total_goals'] == 0
    elif "sem gol primeiro tempo" in desc:
        return metrics['total_first_half_goals'] == 0
    elif "gol de bicicleta" in desc:
        return metrics['total_bicycle'] > 0
    elif "gol de penalti" in desc or "gol de pênalti" in desc:
        return metrics['total_penalties_scored'] > 0
    elif "penalti perdido" in desc or "pênalti perdido" in desc:
        return metrics['total_penalties_missed'] > 0
    elif "ambos marcam - sim" in desc:
        return metrics['home'] > 0 and metrics['away'] > 0
    elif "ambos marcam - nao" in desc or "ambos marcam - não" in desc:
        return metrics['home'] == 0 or metrics['away'] == 0
    elif "expulsões" in desc or "expulsao" in desc or "expulsão" in desc:
        return metrics['total_expulsions'] > 0
    elif "gol de cabeça" in desc and not any(x in desc for x in ["+", "-", "mais", "menos"]):
        return metrics['total_headers'] > 0
    elif "gol no primeiro tempo" in desc and not any(x in desc for x in ["+", "-", "mais", "menos"]):
        return metrics['total_first_half_goals'] > 0
    else:
        nums = re.findall(r'\d+(?:\.\d+)?', desc)
        if nums:
            val = float(nums[0])
            metric_val = get_metric_value_for_cashout(desc, metrics)
                
            if "+" in desc or "mais de" in desc or "over" in desc:
                return metric_val > val
            elif "-" in desc or "menos de" in desc or "under" in desc:
                return metric_val <= val
    return False

# ================= MODELOS DE BANCO DE DADOS =================

bet_odds = db.Table('bet_odds',
    db.Column('bet_id', db.Integer, db.ForeignKey('bet.id'), primary_key=True),
    db.Column('odd_id', db.Integer, db.ForeignKey('odd.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    balance = db.Column(db.Float, default=0.0)
    bets = db.relationship('Bet', backref='user', lazy=True)
    transactions = db.relationship('Transaction', backref='user', lazy=True)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Aberta')
    current_progress = db.Column(db.Integer, default=0)
    home_score = db.Column(db.Integer, default=0)
    away_score = db.Column(db.Integer, default=0) 
    home_headers = db.Column(db.Integer, default=0)
    away_headers = db.Column(db.Integer, default=0)
    home_cards = db.Column(db.Integer, default=0)
    away_cards = db.Column(db.Integer, default=0)
    home_team = db.Column(db.String(100), nullable=False, default="Time Casa")
    away_team = db.Column(db.String(100), nullable=False, default="Time Fora")
    home_logo = db.Column(db.String(500), nullable=True)
    away_logo = db.Column(db.String(500), nullable=True)
    home_expulsions = db.Column(db.Integer, default=0)
    away_expulsions = db.Column(db.Integer, default=0)
    home_corners = db.Column(db.Integer, default=0)
    away_corners = db.Column(db.Integer, default=0)
    home_bicycle_goals = db.Column(db.Integer, default=0)
    away_bicycle_goals = db.Column(db.Integer, default=0)
    home_penalties_scored = db.Column(db.Integer, default=0)
    away_penalties_scored = db.Column(db.Integer, default=0)
    home_penalties_missed = db.Column(db.Integer, default=0)
    away_penalties_missed = db.Column(db.Integer, default=0)
    home_first_half_goals = db.Column(db.Integer, default=0)
    away_first_half_goals = db.Column(db.Integer, default=0)
    period = db.Column(db.String(50), default="Não Iniciado") 
    timer_active = db.Column(db.Boolean, default=False)
    timer_start_time = db.Column(db.DateTime, nullable=True)
    saved_seconds = db.Column(db.Integer, default=0)
    
    odds = db.relationship('Odd', backref='game', lazy=True)

    def get_current_time(self):
        if not self.timer_active or not self.timer_start_time:
            total_seconds = self.saved_seconds
        else:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            start = self.timer_start_time.replace(tzinfo=None)
            elapsed = (now - start).total_seconds()
            total_seconds = self.saved_seconds + int(elapsed)
        return total_seconds // 60, total_seconds % 60
    
    def get_progress_percentage(self):
        # Chama a sua lógica existente para pegar os segundos totais
        minutes, _ = self.get_current_time()
        
        # Calcula a porcentagem baseada em 90 minutos
        # Limitamos a 100 para a barra não passar do final
        percent = (minutes / 90.0) * 100
        return min(max(percent, 0), 100)

class Odd(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    description = db.Column(db.String(100), nullable=False)
    multiplier = db.Column(db.Float, nullable=False)
    is_winner = db.Column(db.Boolean, default=False)

    def is_currently_hitting(self):
        if self.game.status == 'Finalizado':
            return self.is_winner
        metrics = get_game_metrics(self.game)
        return evaluate_odd_logic(self.description, metrics)

class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    total_multiplier = db.Column(db.Float, nullable=False)
    potential_win = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Pendente')
    manual_cashout_value = db.Column(db.Float, nullable=True, default=None)
    odds = db.relationship('Odd', secondary=bet_odds, backref=db.backref('bets', lazy=True))
    cashout_amount = db.Column(db.Float, default=0.0)

    def calculate_live_cashout(self):
        if self.manual_cashout_value is not None:
            return round(self.manual_cashout_value, 2)

        if self.status != 'Pendente':
            return 0.0
        
        pesos_individuais = []
        jogos_no_bilhete = len(self.odds)
        
        if jogos_no_bilhete == 0:
            return 0.0

        for odd in self.odds:
            game = odd.game
            
            # --- CÁLCULO DE TEMPO EM TEMPO REAL ---
            if game.timer_active and game.timer_start_time:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                start = game.timer_start_time.replace(tzinfo=None)
                elapsed_seconds = (game.saved_seconds or 0) + int((now - start).total_seconds())
            else:
                elapsed_seconds = game.saved_seconds or 0
                
            tempo_decorrido = elapsed_seconds / 60.0
            proporcao_tempo = min(tempo_decorrido / 90.0, 1.0)
            # -------------------------------------------

            if game.status == 'Trancada':
                pesos_individuais.append(0.1)
            elif game.status == 'Finalizado':
                pesos_individuais.append(odd.multiplier if odd.is_winner else 0.0)
            else:
                desc = odd.description.lower().strip()
                metrics = get_game_metrics(game)
                
                # 1. AVALIA SE A ODD ESTÁ GANHANDO OU PERDENDO NESTE EXATO MOMENTO
                is_hitting = evaluate_odd_logic(desc, metrics)
                
                # 2. CALCULA A "ESPERANÇA" BASE DA APOSTA
                peso_base = 0.0
                
                # --- LÓGICA DE SALDO DE GOLS PARA VITÓRIAS ---
                if "casa vence" in desc or "fora vence" in desc:
                    gols_favor = metrics['home'] if "casa vence" in desc else metrics['away']
                    gols_contra = metrics['away'] if "casa vence" in desc else metrics['home']
                    saldo = gols_favor - gols_contra
                    
                    if saldo >= 3:
                        peso_base = odd.multiplier * 9.70 
                    elif saldo == 2:
                        peso_base = odd.multiplier * 4.70 
                    elif saldo == 1:
                        peso_base = odd.multiplier * 0.85 
                    elif saldo == 0:
                        # MELHORIA: Aumentado de 0.30 para 0.45 para não zerar fácil
                        peso_base = odd.multiplier * 0.45 
                    elif saldo == -1:
                        # MELHORIA: Aumentado de 0.10 para 0.25
                        peso_base = odd.multiplier * 0.25 
                    else:
                        # MELHORIA: Aumentado de 0.02 para 0.15 (Piso de segurança)
                        peso_base = odd.multiplier * 0.15 
                            
                # --- LÓGICA PARA EMPATE ---
                elif "empate" in desc:
                    saldo = abs(metrics['home'] - metrics['away'])
                    if saldo == 0:
                        peso_base = odd.multiplier * 0.80 
                    elif saldo == 1:
                        peso_base = odd.multiplier * 0.20 
                    else:
                        # MELHORIA: Aumentado de 0.02 para 0.10
                        peso_base = odd.multiplier * 0.10
                        
                # --- MANTÉM AS DEMAIS REGRAS ORIGINAIS ---
                elif "gol de cabeça" in desc and not any(x in desc for x in ["+", "-", "mais", "menos"]):
                    peso_base = odd.multiplier if metrics['total_headers'] > 0 else 0.4
                elif "ambos marcam - sim" in desc:
                    peso_base = odd.multiplier if (metrics['home'] > 0 and metrics['away'] > 0) else 0.4
                elif "ambos marcam - nao" in desc or "ambos marcam - não" in desc:
                    peso_base = odd.multiplier if (metrics['home'] == 0 or metrics['away'] == 0) else 0.05
                elif "expulsões" in desc or "expulsao" in desc:
                    peso_base = odd.multiplier if metrics['total_expulsions'] > 0 else 0.3
                else:
                    nums = re.findall(r'\d+', desc)
                    target = int(nums[0]) if nums else 1
                    current = get_metric_value_for_cashout(desc, metrics)
                    if "+" in desc or "mais de" in desc or "over" in desc:
                        peso_base = odd.multiplier if current > target else (1.0 * (current / (target + 1)))
                    else: 
                        peso_base = odd.multiplier if current <= target else 0.05

                # 3. A MÁGICA: O TEMPO É AMIGO OU INIMIGO?
                if is_hitting:
                    fator_tempo = 0.70 + (proporcao_tempo * 0.30) 
                else:
                    # MELHORIA: Reduzido o impacto da punição do tempo
                    fator_tempo = max(0.20, 1.0 - proporcao_tempo)
                    
                peso_final = peso_base * fator_tempo
                pesos_individuais.append(peso_final)

        # Tira a média dos pesos de todos os jogos do bilhete
        total_weight = sum(pesos_individuais) / jogos_no_bilhete
        
        # MELHORIA: Piso de recuperação elevado de 10% para 20%
        cashout_value = max(self.amount * 0.20, self.amount * total_weight)
        
        # Teto de segurança (A casa nunca paga mais que 95% do prêmio potencial no cashout antecipado)
        return round(min(cashout_value, self.potential_win * 0.95), 2)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default='Pendente')
    date = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ================= FUNÇÃO DE LIQUIDAÇÃO ANTECIPADA AUTOMÁTICA =================

def check_and_settle_live_bets(game):
    if game.status != 'Finalizado':
        return

    pending_bets = Bet.query.filter_by(status='Pendente').all()
    
    for bet in pending_bets:
        if not any(o.game_id == game.id for o in bet.odds):
            continue

        all_games_finished = all(odd.game.status == 'Finalizado' for odd in bet.odds)
        
        if all_games_finished:
            ticket_won = True
            for odd in bet.odds:
                if not odd.is_winner:
                    ticket_won = False
                    break
            
            if ticket_won:
                bet.status = 'Ganhou'
                bet.user.balance += bet.potential_win
            else:
                bet.status = 'Perdeu'
            
            db.session.commit()

# ================= ROTAS DE AUTENTICAÇÃO E CONTA =================

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))
        flash('Usuário ou senha inválidos.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Este usuário já existe.')
            return redirect(url_for('register'))
        
        is_admin = False
        if User.query.count() == 0:
            is_admin = True
            
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, is_admin=is_admin, balance=0.0)
        db.session.add(new_user)
        db.session.commit()
        flash('Cadastro realizado com sucesso! Faça login.')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/finance', methods=['GET', 'POST'])
@login_required
def finance():
    if request.method == 'POST':
        amount = float(request.form.get('amount', 0))
        tx_type = request.form.get('type') 
        
        if amount > 0 and tx_type in ['Deposito', 'Saque']:
            new_tx = Transaction(
                user_id=current_user.id, 
                amount=amount, 
                type=tx_type,
                status='Pendente'
            )
            db.session.add(new_tx)
            db.session.commit()
            flash(f'Solicitação de {tx_type} enviada com sucesso!')
        else:
            flash('Erro: Valor inválido ou tipo de transação não selecionado.')
            
    txs = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.date.desc()).all()
    return render_template('finance.html', transactions=txs)

# ================= PAINEL DO USUÁRIO & CASHOUT =================

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        odd_ids = request.form.getlist('odds')
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            amount = 0.0

        if amount <= 0 or not odd_ids:
            flash('Selecione ao menos um palpite e insira um valor válido.')
            return redirect(url_for('dashboard'))
            
        if current_user.balance < amount:
            flash('Saldo insuficiente!')
            return redirect(url_for('dashboard'))

        total_multiplier = 1.0
        odds_objects = []
        for o_id in odd_ids:
            odd_obj = db.session.get(Odd, int(o_id))
            if odd_obj:
                if odd_obj.game.status in ['Trancada', 'Finalizado']:
                    flash(f'O mercado para o jogo "{odd_obj.game.title}" está fechado/suspenso.')
                    return redirect(url_for('dashboard'))
                odds_objects.append(odd_obj)
                total_multiplier *= odd_obj.multiplier
                
        current_user.balance -= amount
        new_bet = Bet(
            user_id=current_user.id,
            amount=amount,
            total_multiplier=round(total_multiplier, 2),
            potential_win=round(amount * total_multiplier, 2),
            status='Pendente'
        )
        new_bet.odds.extend(odds_objects)
        db.session.add(new_bet)
        db.session.commit()
        flash('Bilhete registrado com sucesso!')
        return redirect(url_for('bet_history'))

    # ... (mantenha a lógica de POST do dashboard intacta) ...

    games = Game.query.filter(Game.status != 'Finalizado').all()
    
    # Busca as apostas pendentes do usuário atual para exibir no checklist
    pending_bets = Bet.query.filter_by(user_id=current_user.id, status='Pendente').order_by(Bet.id.desc()).all()
    
    # Renderização empacotada passando também as pending_bets
    response = make_response(render_template('dashboard.html', games=games, pending_bets=pending_bets))
    
    # Nova lógica do Auto-Refresh Global (15 segundos)
    if app.config.get('GLOBAL_AUTO_REFRESH', False):
        response.headers['Refresh'] = '15'
        
    return response

@app.route('/admin/toggle_refresh', methods=['POST'])
@login_required
def toggle_refresh():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    
    # Inverte o estado atual (Se for False vira True, e vice-versa)
    app.config['GLOBAL_AUTO_REFRESH'] = not app.config.get('GLOBAL_AUTO_REFRESH', False)
    
    estado = "ATIVADO" if app.config['GLOBAL_AUTO_REFRESH'] else "DESATIVADO"
    flash(f'Auto-Refresh Global {estado}!')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/bet_history')
@login_required
def bet_history():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.id.desc()).all()
    return render_template('bet_history.html', bets=bets)


    #rota de api 877777777777777777777777777777777777777777777777777777777777777777777776

# 1. ROTA DE API (Para o seu JS no dashboard)
@app.route('/api/get_live_cashouts')
@login_required
def get_live_cashouts():
    bets = Bet.query.filter_by(user_id=current_user.id, status='Pendente').all()
    # Retorna o valor atualizado para cada aposta pendente
    return jsonify({b.id: b.calculate_live_cashout() for b in bets})

# 2. LÓGICA DE FINALIZAÇÃO (Para o Histórico)
def verificar_resultado_aposta(bet):
    for odd in bet.odds:
        game = odd.game
        # Aqui entra sua regra: se o resultado do jogo não bater com a odd, aposta perdida
        if not game.is_winner(odd): 
            bet.status = 'Perdida'
            return False
    bet.status = 'Ganha'
    return True

# 2. ROTA DE CASHOUT (Ajustada para salvar o valor histórico)
@app.route('/bet/cashout/<int:bet_id>', methods=['POST'])
@login_required
def cashout(bet_id):
    bet = db.session.get(Bet, bet_id)
    
    if not bet or bet.user_id != current_user.id or bet.status != 'Pendente':
        return jsonify(success=False, message="Cash Out indisponível.")
    
    for odd in bet.odds:
        if odd.game.status in ['Trancada', 'Finalizado']:
            return jsonify(success=False, message="Mercado suspenso ou finalizado.")
            
    valor_final = bet.calculate_live_cashout()
    if valor_final <= 0:
        return jsonify(success=False, message="Valor indisponível.")

    try:
        bet.status = 'Cashout'
        bet.cashout_amount = float(valor_final) # <--- SALVA O VALOR NO HISTÓRICO
        
        if hasattr(current_user, 'saldo'):
            current_user.saldo += float(valor_final)
        else:
            current_user.balance += float(valor_final)
            
        db.session.commit()
        return jsonify(success=True, message=f"R$ {valor_final:.2f} creditados!")
        
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message="Erro interno ao processar Cash Out.")

# ================= PAINEL ADMINISTRATIVO & LIQUIDAÇÃO =================

@app.route('/admin/dashboard', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        title_input = request.form.get('title') 
        if title_input:
            if " x " in title_input.lower():
                teams = title_input.split(' x ')
            elif " vs " in title_input.lower():
                teams = title_input.split(' vs ')
            else:
                teams = [title_input, "Time Visitante"]
            
            home_team = teams[0].strip()
            away_team = teams[1].strip() if len(teams) > 1 else "Time Visitante"
            
            logos_database = {
                "corinthians": "https://cdn.freebiesupply.com/logos/large/2x/esporte-clube-corinthians-de-andradina-sp-logo-png-transparent.png",
                "sao paulo": "https://logodetimes.com/times/sao-paulo/logo-sao-paulo-4096.png",
                "palmeiras": "https://static.wikia.nocookie.net/cftu/images/c/cd/Palmeiras.png/revision/latest/thumbnail/width/360/height/450?cb=20170102174540&path-prefix=pt-br",
                "santos": "https://upload.wikimedia.org/wikipedia/commons/1/15/Santos_Logo.png",
                "flamengo": "https://logodetimes.com/times/flamengo/logo-flamengo-1536.png",
                "real madrid": "https://upload.wikimedia.org/wikipedia/ar/thumb/5/56/Real_Madrid_CF.svg/330px-Real_Madrid_CF.svg.png",
                "brasil":"https://logodetimes.com/times/selecao-brasileira-brasil-novo-logo-2019/logo-selecao-brasileira-brasil-novo-logo-2019-4096.png",
                "argentina":"https://logodetimes.com/times/argentina/selecao-argentina-de-futebol-4096.png",
                "frança":"https://logodownload.org/wp-content/uploads/2023/06/bandeira-france-flag-0.png",
                "alemanha":"https://logodetimes.com/times/alemanha/selecao-alema-de-futebol-2048.png",
                "marrocos":"https://www.wikisporting.com/images/f/f8/Marrocos.png",
                "espanha":"https://logodownload.org/wp-content/uploads/2023/06/bandeira-espanha-flag.png",
                "belgica":"https://logodownload.org/wp-content/uploads/2023/08/bandeira-belgium-flag-2.png",
                "noruega":"https://images.emojiterra.com/twitter/v14.0/1024px/1f1f3-1f1f4.png",
                "inglaterra":"https://logodownload.org/wp-content/uploads/2023/07/bandeira-england-flag.png",
                "argentina":"https://upload.wikimedia.org/wikipedia/commons/8/8f/Flag_of_Argentina.png",
                "suiça":"https://e7.pngegg.com/pngimages/611/733/png-clipart-computer-icons-switzerland-swiss-flag-rectangle-switzerland.png"
            }
            
            home_logo = logos_database.get(home_team.lower(), "/static/img/default.png")
            away_logo = logos_database.get(away_team.lower(), "/static/img/default.png")
            
            novo_jogo = Game(
                title=title_input,
                home_team=home_team,
                away_team=away_team,
                home_logo=home_logo,
                away_logo=away_logo,
                status='Aberta'
            )
            
            db.session.add(novo_jogo)
            db.session.commit()
            
            for opcao in OPCOES_PADRAO:
                nova_odd = Odd(game_id=novo_jogo.id, description=opcao, multiplier=2.00)
                db.session.add(nova_odd)
            
            db.session.commit()
            flash("Partida e mercados criados com sucesso!")
            return redirect(url_for('admin_dashboard'))

    games = Game.query.order_by(Game.id.desc()).all()
    all_bets = Bet.query.order_by(Bet.id.desc()).all()
    
    pending_transactions = Transaction.query.filter_by(status='Pendente').all()
    pending_deposits = Transaction.query.filter(Transaction.type.ilike('%deposito%'), Transaction.status == 'Pendente').all()
    pending_withdrawals = Transaction.query.filter(Transaction.type.ilike('%saque%'), Transaction.status == 'Pendente').all()
    
    return render_template(
        'admin_dashboard.html', 
        games=games, 
        all_bets=all_bets, 
        pending_transactions=pending_transactions,
        deposits=pending_deposits, 
        withdrawals=pending_withdrawals
    )

@app.route('/admin/update_game_progress/<int:game_id>', methods=['POST'])
@login_required
def update_game_progress(game_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    game.current_progress = int(request.form.get('current_progress', 0))
    db.session.commit()
    
    check_and_settle_live_bets(game)
    
    flash(f'Progresso do jogo "{game.title}" atualizado!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_game_score/<int:game_id>', methods=['POST'])
@login_required
def update_game_score(game_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    
    game.home_score = int(request.form.get('home_score', 0))
    game.away_score = int(request.form.get('away_score', 0))
    game.home_headers = int(request.form.get('home_headers', 0))
    game.away_headers = int(request.form.get('away_headers', 0))
    game.home_cards = int(request.form.get('home_cards', 0))
    game.away_cards = int(request.form.get('away_cards', 0))
    game.home_corners = int(request.form.get('home_corners', 0))
    game.away_corners = int(request.form.get('away_corners', 0))
    game.home_bicycle_goals = int(request.form.get('home_bicycle_goals', 0))
    game.away_bicycle_goals = int(request.form.get('away_bicycle_goals', 0))
    game.home_penalties_scored = int(request.form.get('home_penalties_scored', 0))
    game.away_penalties_scored = int(request.form.get('away_penalties_scored', 0))
    game.home_penalties_missed = int(request.form.get('home_penalties_missed', 0))
    game.away_penalties_missed = int(request.form.get('away_penalties_missed', 0))
    game.home_first_half_goals = int(request.form.get('home_first_half_goals', 0))
    game.away_first_half_goals = int(request.form.get('away_first_half_goals', 0))
    game.home_expulsions = int(request.form.get('home_expulsions', 0))
    game.away_expulsions = int(request.form.get('away_expulsions', 0))
    
    game.current_progress = game.home_score + game.away_score
    
    print(f"\n--- 🔄 ATUALIZANDO JOGO: {game.title} ---")
    
    metrics = get_game_metrics(game)
    for odd in game.odds:
        odd.is_winner = evaluate_odd_logic(odd.description, metrics)
        print(f"    Resultado definido para '{odd.description}': {odd.is_winner}")

    db.session.commit()
    check_and_settle_live_bets(game)
    
    flash(f'Placar de "{game.title}" modificado para {game.home_score}x{game.away_score}!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/force_cashout_value/<int:bet_id>', methods=['POST'])
@login_required
def force_cashout_value(bet_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    bet = db.session.get(Bet, bet_id)
    if bet:
        manual_val = request.form.get('manual_value')
        if manual_val and manual_val.strip() != "":
            bet.manual_cashout_value = float(manual_val)
            flash(f'Cash out do bilhete #{bet.id} travado em R$ {float(manual_val):.2f}!')
        else:
            bet.manual_cashout_value = None
            flash(f'Cash out do bilhete #{bet.id} redefinido para cálculo automático.')
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/create_game', methods=['POST'])
@login_required
def create_game():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
        
    title_input = request.form.get('title')
    initial_multiplier = request.form.get('initial_multiplier', type=float, default=2.00)
    
    if title_input:
        if " x " in title_input.lower():
            teams = title_input.split(' x ')
        elif " vs " in title_input.lower():
            teams = title_input.split(' vs ')
        else:
            teams = [title_input, ""]
        
        home_team = teams[0].strip()
        away_team = teams[1].strip() if len(teams) > 1 else "Visitante"
        
        logos_database = {
                "corinthians": "https://cdn.freebiesupply.com/logos/large/2x/esporte-clube-corinthians-de-andradina-sp-logo-png-transparent.png",
                "sao paulo": "https://logodetimes.com/times/sao-paulo/logo-sao-paulo-4096.png",
                "palmeiras": "https://static.wikia.nocookie.net/cftu/images/c/cd/Palmeiras.png/revision/latest/thumbnail/width/360/height/450?cb=20170102174540&path-prefix=pt-br",
                "santos": "https://upload.wikimedia.org/wikipedia/commons/1/15/Santos_Logo.png",
                "flamengo": "https://logodetimes.com/times/flamengo/logo-flamengo-1536.png",
                "real madrid": "https://upload.wikimedia.org/wikipedia/ar/thumb/5/56/Real_Madrid_CF.svg/330px-Real_Madrid_CF.svg.png",
                "brasil":"https://logodetimes.com/times/selecao-brasileira-brasil-novo-logo-2019/logo-selecao-brasileira-brasil-novo-logo-2019-4096.png",
                "argentina":"https://logodetimes.com/times/argentina/selecao-argentina-de-futebol-4096.png",
                "frança":"https://logodownload.org/wp-content/uploads/2023/06/bandeira-france-flag-0.png",
                "alemanha":"https://logodetimes.com/times/alemanha/selecao-alema-de-futebol-2048.png",
                "marrocos":"https://www.wikisporting.com/images/f/f8/Marrocos.png",
                "espanha":"https://logodownload.org/wp-content/uploads/2023/06/bandeira-espanha-flag.png",
                "belgica":"https://logodownload.org/wp-content/uploads/2023/08/bandeira-belgium-flag-2.png",
                "noruega":"https://images.emojiterra.com/twitter/v14.0/1024px/1f1f3-1f1f4.png",
                "inglaterra":"https://logodownload.org/wp-content/uploads/2023/07/bandeira-england-flag.png",
                "argentina":"https://upload.wikimedia.org/wikipedia/commons/8/8f/Flag_of_Argentina.png",
                "suiça":"https://e7.pngegg.com/pngimages/611/733/png-clipart-computer-icons-switzerland-swiss-flag-rectangle-switzerland.png"
        }
        
        home_logo = logos_database.get(home_team.lower(), "/static/img/default.png")
        away_logo = logos_database.get(away_team.lower(), "/static/img/default.png")
        
        novo_jogo = Game(
            title=title_input,
            home_team=home_team,
            away_team=away_team,
            home_logo=home_logo,
            away_logo=away_logo,
            status='Aberta'
        )
        
        db.session.add(novo_jogo)
        db.session.commit()
        
        for opcao in OPCOES_PADRAO:
            nova_odd = Odd(game_id=novo_jogo.id, description=opcao, multiplier=initial_multiplier)
            db.session.add(nova_odd)
            
        db.session.commit()
        flash("Partida criada com sucesso!")
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_odds/<int:game_id>', methods=['POST'])
@login_required
def update_odds(game_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game:
        for odd in game.odds:
            new_val = request.form.get(f'odd_val_{odd.id}')
            if new_val: odd.multiplier = round(float(new_val), 2)
        db.session.commit()
        flash('Odds salvas com sucesso!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/change_status/<int:game_id>/<string:new_status>')
@login_required
def change_status(game_id, new_status):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game and new_status in ['Aberta', 'Ao Vivo', 'Trancada', 'Finalizado']:
        game.status = new_status
        db.session.commit()
        flash(f'Status do jogo alterado para {new_status}!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_game/<int:game_id>', methods=['POST'])
@login_required
def delete_game(game_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    game = db.session.get(Game, game_id)
    if game and game.status == 'Finalizado':
        for odd in game.odds:
            db.session.execute(bet_odds.delete().where(bet_odds.c.odd_id == odd.id))
            db.session.delete(odd)
        db.session.delete(game)
        db.session.commit()
        flash('Jogo finalizado e seu histórico foram excluídos permanentemente!')
    else:
        flash('Erro: Apenas partidas com status "Finalizado" podem ser excluídas.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settle_game/<int:game_id>', methods=['POST'])
@login_required
def settle_game(game_id):
    if not getattr(current_user, 'is_admin', False):
        return "Não autorizado", 403
        
    game = db.session.get(Game, game_id)
    if not game:
        return "Jogo não encontrado", 404

    if game.period != "Fim de Jogo":
        flash(f"Atenção: O jogo '{game.title}' está no período '{game.period}'. Mude para 'Fim de Jogo' antes de liquidar!", "danger")
        return redirect(url_for('admin_dashboard'))
    
    if game.status == 'Finalizado':
        flash("Este jogo já foi liquidado anteriormente.", "warning")
        return redirect(url_for('admin_dashboard'))

    metrics = get_game_metrics(game)
    for odd in game.odds:
        odd.is_winner = evaluate_odd_logic(odd.description, metrics)

    game.status = 'Finalizado'
    
    try:
        pending_bets = Bet.query.filter_by(status='Pendente').all()
        
        for bet in pending_bets:
            if any(odd.game_id == game.id for odd in bet.odds):
                if all(o.game.status == 'Finalizado' for o in bet.odds):
                    ticket_won = all(odd.is_winner for odd in bet.odds)
                    
                    if ticket_won:
                        bet.status = 'Ganhou'
                        valor_premio = float(bet.potential_win)
                        if hasattr(bet.user, 'saldo'):
                            bet.user.saldo += valor_premio
                        elif hasattr(bet.user, 'balance'):
                            bet.user.balance += valor_premio
                    else:
                        bet.status = 'Perdeu'
        
        db.session.commit()
        flash(f"O confronto '{game.title}' foi finalizado e todos os bilhetes processados.", "success")
        
    except Exception as e:
        db.session.rollback()
        flash(f"Erro ao processar liquidação: {str(e)}", "danger")
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_transaction/<int:tx_id>', methods=['POST'])
@login_required
def approve_transaction(tx_id):
    if not current_user.is_admin: 
        return redirect(url_for('dashboard'))
        
    tx = db.session.get(Transaction, tx_id)
    
    if not tx:
        flash("Transação não encontrada.")
        return redirect(url_for('admin_dashboard'))

    if tx.status != 'Pendente':
        flash("Esta transação já foi processada.")
        return redirect(url_for('admin_dashboard'))

    tipo = str(tx.type).strip().capitalize()

    if tipo == 'Deposito':
        tx.user.balance += tx.amount
        tx.status = 'Aprovado'
        flash(f"Depósito de R$ {tx.amount:.2f} aprovado com sucesso!")
            
    elif tipo == 'Saque':
        if tx.user.balance >= tx.amount:
            tx.user.balance -= tx.amount
            tx.status = 'Aprovado'
            flash(f"Saque de R$ {tx.amount:.2f} aprovado com sucesso!")
        else:
            flash(f"Erro: Usuário não tem saldo suficiente para este saque!")
            return redirect(url_for('admin_dashboard'))
    
    else:
        flash(f"Erro: Tipo de transação desconhecido ('{tipo}').")
        return redirect(url_for('admin_dashboard'))
                
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reject_transaction/<int:tx_id>', methods=['POST'])
@login_required
def reject_transaction(tx_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    tx = db.session.get(Transaction, tx_id)
    if tx and tx.status == 'Pendente':
        tx.status = 'Rejeitado'
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle_odd_winner/<int:odd_id>', methods=['POST'])
@login_required
def toggle_odd_winner(odd_id):
    if not getattr(current_user, 'is_admin', False):
        return jsonify({'success': False, 'message': 'Não autorizado'}), 403
        
    odd = db.session.get(Odd, odd_id) 
    if not odd:
        return jsonify({'success': False, 'message': 'Odd não encontrada'}), 404
        
    odd.is_winner = not odd.is_winner
    db.session.commit()
    
    return jsonify({'success': True, 'is_winner': odd.is_winner})

@app.template_filter('brl')
def brl_filter(valor):
    if valor is None: return "0,00"
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

@app.route('/admin/control_timer/<int:game_id>/<string:action>', methods=['POST'])
@login_required
def control_timer(game_id, action):
    if not getattr(current_user, 'is_admin', False):
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    
    if action == 'start':
        if not game.timer_active:
            game.timer_active = True
            game.timer_start_time = datetime.now(timezone.utc).replace(tzinfo=None)
            if game.period == "Não Iniciado":
                game.period = "1º Tempo"
            flash(f'Cronômetro de "{game.title}" iniciado!')
    elif action == 'pause':
        if game.timer_active and game.timer_start_time:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed = (now - game.timer_start_time).total_seconds()
            game.saved_seconds += int(elapsed)
            game.timer_active = False
            game.timer_start_time = None
            flash(f'Cronômetro de "{game.title}" pausado!')
    elif action == 'reset':
        game.timer_active = False
        game.timer_start_time = None
        game.saved_seconds = 0
        game.period = "Não Iniciado"
        flash(f'Cronômetro de "{game.title}" reiniciado!')
    elif action == 'next_period':
        if game.timer_active and game.timer_start_time:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed = (now - game.timer_start_time).total_seconds()
            game.saved_seconds += int(elapsed)
            game.timer_start_time = datetime.now(timezone.utc).replace(tzinfo=None)
            
        if game.period == "1º Tempo":
            game.period = "Intervalo"
        elif game.period == "Intervalo":
            game.period = "2º Tempo"
        elif game.period == "2º Tempo":
            game.period = "Fim de Jogo"
        flash(f'Período de "{game.title}" alterado para {game.period}!')
        
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
