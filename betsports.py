import os
import re
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecreto123'
# Puxa o link do Neon na nuvem; se não achar, usa o SQLite local
db_url = os.environ.get('DATABASE_URL', 'sqlite:///betsports.db')

# Correção necessária: o SQLAlchemy exige que comece com 'postgresql://', 
# mas algumas nuvens entregam como 'postgres://'
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

OPCOES_PADRAO = [
    "Casa vence", "Empate", "Fora vence",
    "+ 1 gol", "+ 2 gols", "+ 3 gols", "+ 4 gols", "+ 5 gols",
    "- 1 gol", "- 2 gols", "- 3 gols", "- 4 gols", "- 5 gols",
    "Gol de cabeça", "Sem gols", "+ 2 Cartões", "Expulsões"
]

# ================= MODELOS DE BANCO DE DADOS =================

bet_odds = db.Table('bet_odds',
    db.Column('bet_id', db.Integer, db.ForeignKey('bet.id'), primary_key=True),
    db.Column('odd_id', db.Integer, db.ForeignKey('odd.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    balance = db.Column(db.Float, default=0.0)
    bets = db.relationship('Bet', backref='user', lazy=True)
    transactions = db.relationship('Transaction', backref='user', lazy=True)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Aberta')  # Aberta, Ao Vivo, Trancada, Finalizado
    current_progress = db.Column(db.Integer, default=0)  # Quantidade de gols live
    home_score = db.Column(db.Integer, default=0)        # Gols do time da Casa
    away_score = db.Column(db.Integer, default=0)        # Gols do time Fora
    odds = db.relationship('Odd', backref='game', lazy=True)

class Odd(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    description = db.Column(db.String(100), nullable=False)
    multiplier = db.Column(db.Float, nullable=False)
    is_winner = db.Column(db.Boolean, default=False)

    def is_currently_hitting(self):
        """Avalia se este palpite específico está se concretizando no momento atual da partida"""
        if self.game.status == 'Finalizado':
            return self.is_winner
            
        home = self.game.home_score or 0
        away = self.game.away_score or 0
        total_gols = self.game.current_progress or 0
        
        if "Casa vence" in self.description:
            return home > away
        elif "Fora vence" in self.description:
            return away > home
        elif "Empate" in self.description:
            return home == away
        elif "+" in self.description:
            nums = re.findall(r'\d+', self.description)
            return total_gols > int(nums[0]) if nums else False  # Ajustado de >= para > conforme regra de mercado
        elif "-" in self.description:
            nums = re.findall(r'\d+', self.description)
            return total_gols <= int(nums[0]) if nums else False
        return False

class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    total_multiplier = db.Column(db.Float, nullable=False)
    potential_win = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Pendente')  # Pendente, Ganhou, Perdeu, Cashout
    manual_cashout_value = db.Column(db.Float, nullable=True, default=None)
    odds = db.relationship('Odd', secondary=bet_odds, backref=db.backref('bets', lazy=True))

    def calculate_live_cashout(self):
        """Calcula o Cash Out dinâmico baseado no progresso real dos jogos"""
        if self.manual_cashout_value is not None:
            return round(self.manual_cashout_value, 2)

        if self.status != 'Pendente':
            return 0.0
        
        total_weight = 1.0
        
        for odd in self.odds:
            game = odd.game
            if game.status == 'Trancada':
                total_weight *= 0.1
                continue
            elif game.status == 'Finalizado':
                total_weight *= 0.5
                continue
            
            home = game.home_score or 0
            away = game.away_score or 0
            
            if "Casa vence" in odd.description:
                if home > away:
                    vantagem = home - away
                    total_weight *= (1.0 + (vantagem * 0.25))
                elif home == away:
                    total_weight *= 0.70
                else:
                    desvantagem = away - home
                    total_weight *= max(0.05, 0.35 - (desvantagem * 0.15))

            elif "Fora vence" in odd.description:
                if away > home:
                    vantagem = away - home
                    total_weight *= (1.0 + (vantagem * 0.25))
                elif home == away:
                    total_weight *= 0.70
                else:
                    desvantagem = home - away
                    total_weight *= max(0.05, 0.35 - (desvantagem * 0.15))

            elif "Empate" in odd.description:
                if home == away:
                    total_weight *= 1.10
                else:
                    distancia = abs(home - away)
                    total_weight *= max(0.05, 0.40 - (distancia * 0.20))
                    
            else:
                numbers = re.findall(r'\d+', odd.description)
                target = int(numbers[0]) if numbers else 1
                current = game.current_progress or 0
                
                if "+" in odd.description:
                    if current > target:  # Se já bateu a meta (+2 gols com 3 marcados)
                        total_weight *= odd.multiplier
                    else:
                        proximity = current / (target + 1) if target >= 0 else 0
                        partial_multiplier = 1.0 + (odd.multiplier - 1.0) * proximity * 0.65
                        total_weight *= partial_multiplier
                else:
                    if current <= target:
                        total_weight *= odd.multiplier
                    else:
                        total_weight *= 0.05
                
        cashout_value = self.amount * total_weight * 0.80
        if cashout_value > self.potential_win:
            cashout_value = self.potential_win * 0.85
            
        return round(cashout_value, 2)

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
    """
    Varre os bilhetes pendentes sempre que há um gol para verificar se 
    mercados irreversíveis (como '+ Gols') já ganharam e realiza o pagamento imediato.
    """
    # 1. Atualiza as odds do jogo atual se elas já bateram a meta de gols de forma irreversível
    for odd in game.odds:
        if "+" in odd.description:
            nums = re.findall(r'\d+', odd.description)
            if nums:
                target = int(nums[0])
                if game.current_progress > target:
                    odd.is_winner = True

    # 2. Varre os bilhetes pendentes do sistema
    pending_bets = Bet.query.filter_by(status='Pendente').all()
    for bet in pending_bets:
        # Só avalia se o bilhete tiver pelo menos uma aposta neste jogo que mudou o placar
        if not any(o.game_id == game.id for o in bet.odds):
            continue

        all_resolved_and_won = True
        for o in bet.odds:
            # Se for um mercado '+' e bateu mid-game, o loop acima marcou o.is_winner = True
            # Se o jogo não terminou e a odd ainda não está ganha, o bilhete continua pendente
            if o.game.status != 'Finalizado' and not o.is_winner:
                all_resolved_and_won = False
                break
            # Se um dos jogos do bilhete já terminou e a odd perdeu, invalida o bilhete
            elif o.game.status == 'Finalizado' and not o.is_winner:
                bet.status = 'Perdeu'
                all_resolved_and_won = False
                break

        # Se todas as seleções do bilhete (simples ou múltipla) constarem como ganhas, paga imediatamente
        if all_resolved_and_won:
            bet.status = 'Ganhou'
            bet.user.balance += bet.potential_win

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
        if amount > 0:
            new_tx = Transaction(user_id=current_user.id, amount=amount, type='Deposito', status='Pendente')
            db.session.add(new_tx)
            db.session.commit()
            flash('Solicitação de depósito enviada! Aguarde a aprovação do Admin.')
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

    games = Game.query.filter(Game.status != 'Finalizado').all()
    return render_template('dashboard.html', games=games)

@app.route('/bet_history')
@login_required
def bet_history():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.id.desc()).all()
    return render_template('bet_history.html', bets=bets)

@app.route('/bet/cashout/<int:bet_id>', methods=['POST'])
@login_required
def cashout(bet_id):
    bet = db.session.get(Bet, bet_id)
    if not bet or bet.user_id != current_user.id or bet.status != 'Pendente':
        flash('Não foi possível processar o cash out.')
        return redirect(url_for('bet_history'))
    
    for odd in bet.odds:
        if odd.game.status == 'Trancada':
            flash('Cash Out indisponível no momento: Mercados suspensos.')
            return redirect(url_for('bet_history'))
        if odd.game.status == 'Finalizado':
            flash('A partida já terminou. Aguarde a liquidação.')
            return redirect(url_for('bet_history'))
            
    cashout_value = bet.calculate_live_cashout()

    current_user.balance += cashout_value
    bet.status = 'Cashout'
    db.session.commit()
    flash(f'Cash Out realizado! R$ {cashout_value:.2f} foram adicionados à sua conta.')
    return redirect(url_for('bet_history'))

# ================= PAINEL ADMINISTRATIVO & LIQUIDAÇÃO =================



@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    games = Game.query.order_by(Game.id.desc()).all()
    pending_transactions = Transaction.query.filter_by(status='Pendente').all()
    all_bets = Bet.query.order_by(Bet.id.desc()).all()
    return render_template('admin_dashboard.html', games=games, pending_transactions=pending_transactions, all_bets=all_bets)

@app.route('/admin/update_game_progress/<int:game_id>', methods=['POST'])
@login_required
def update_game_progress(game_id):
    if not getattr(current_user, 'is_admin', False): 
        return redirect(url_for('dashboard'))
        
    game = Game.query.get_or_404(game_id)
    game.current_progress = int(request.form.get('current_progress', 0))
    db.session.commit()
    
    # 🔥 Gatilho de checagem imediata pós-alteração de progresso
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
    game.current_progress = game.home_score + game.away_score
    db.session.commit()
    
    # 🔥 Gatilho de checagem imediata pós-alteração do placar de gols
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

@app.route('/admin/create_game', methods=['POST'])
@login_required
def create_game():
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    title = request.form.get('title')
    try:
        initial_multiplier = float(request.form.get('initial_multiplier', 2.00))
    except ValueError:
        initial_multiplier = 2.00

    if title:
        new_game = Game(title=title, status='Aberta', home_score=0, away_score=0)
        db.session.add(new_game)
        db.session.flush()
        for mercado in OPCOES_PADRAO:
            db.session.add(Odd(game_id=new_game.id, description=mercado, multiplier=initial_multiplier))
        db.session.commit()
        flash(f'Jogo criado com odds iniciais definidas em {initial_multiplier:.2f}!')
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
        
    # ====================================================================
    # STEP 1: CALCULA E GRAVA AUTOMATICAMENTE OS VENCEDORES NO BANCO
    # ====================================================================
    home = game.home_score or 0
    away = game.away_score or 0
    total_gols = game.current_progress or 0

    for odd in game.odds:
        if "Casa vence" in odd.description:
            odd.is_winner = (home > away)
        elif "Fora vence" in odd.description:
            odd.is_winner = (away > home)
        elif "Empate" in odd.description:
            odd.is_winner = (home == away)
        elif "+" in odd.description:
            nums = re.findall(r'\d+', odd.description)
            odd.is_winner = (total_gols > int(nums[0])) if nums else False
        elif "-" in odd.description:
            nums = re.findall(r'\d+', odd.description)
            odd.is_winner = (total_gols <= int(nums[0])) if nums else False
        elif "Sem gols" in odd.description:
            odd.is_winner = (total_gols == 0)
        # Mercados manuais (ex: 'Gol de cabeça', 'Expulsões') não são alterados aqui,
        # eles continuam valendo o que o admin clicou no painel dinâmico.

    # 2. Modifica o status do jogo para Finalizado
    game.status = 'Finalizado'
    db.session.commit() # Salva os resultados reais das odds primeiro
    
    # ====================================================================
    # STEP 2: VALIDAÇÃO DOS BILHETES PENDENTES
    # ====================================================================
    pending_bets = Bet.query.filter_by(status='Pendente').all()
    
    for bet in pending_bets:
        # Verifica se este bilhete contém alguma aposta relacionada com o jogo finalizado
        if any(odd.game_id == game.id for odd in bet.odds):
            
            all_games_finished = True
            ticket_won = True
            
            # Varre cada palpite dentro do bilhete do utilizador
            for odd in bet.odds:
                # Se o jogo de alguma das odds do bilhete ainda não terminou, o bilhete continua Pendente
                if odd.game.status != 'Finalizado':
                    all_games_finished = False
                    continue
                
                # Se o jogo terminou e a odd NÃO foi marcada como vencedora (is_winner == False)
                if not odd.is_winner:
                    ticket_won = False
                    bet.status = 'Perdeu'
                    break # Se errou uma das seleções, o bilhete inteiro é marcado como Perdeu
            
            # Se todos os jogos do bilhete já terminaram e TODOS foram marcados como Green
            if all_games_finished and ticket_won:
                bet.status = 'Ganhou'
                
                # Paga o utilizador adicionando o valor ao saldo/balance
                if bet.user:
                    if hasattr(bet.user, 'saldo'):
                        bet.user.saldo += bet.potential_win
                    elif hasattr(bet.user, 'balance'):
                        bet.user.balance += bet.potential_win

    # Grava todas as atualizações de bilhetes e saldos no banco de dados
    db.session.commit()
    
    flash(f"O confronto '{game.title}' foi finalizado com sucesso e os bilhetes foram processados.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_transaction/<int:tx_id>', methods=['POST'])
@login_required
def approve_transaction(tx_id):
    if not current_user.is_admin: return redirect(url_for('dashboard'))
    tx = db.session.get(Transaction, tx_id)
    if tx and tx.status == 'Pendente':
        tx.status = 'Aprovado'
        tx.user.balance += tx.amount
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

from flask import jsonify # Garante que tens o jsonify importado

@app.route('/admin/toggle_odd_winner/<int:odd_id>', methods=['POST'])
@login_required
def toggle_odd_winner(odd_id):
    # Verifica se o utilizador é administrador
    if not getattr(current_user, 'is_admin', False):
        return jsonify({'success': False, 'message': 'Não autorizado'}), 403
        
    # Procura a odd no banco de dados
    # Se usares uma versão antiga do SQLAlchemy, usa: odd = Odd.query.get(odd_id)
    odd = db.session.get(Odd, odd_id) 
    if not odd:
        return jsonify({'success': False, 'message': 'Odd não encontrada'}), 404
        
    # Inverte o estado: se era False vira True, se era True vira False
    odd.is_winner = not odd.is_winner
    
    # Salva a alteração imediatamente no banco de dados
    db.session.commit()
    
    return jsonify({'success': True, 'is_winner': odd.is_winner})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
