"""Tela de configurações, incluindo a seção de privacidade."""

from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import Settings
from ...core import imaging
from ...core.fileops import HAS_SEND2TRASH, FileManager, format_bytes
from ...db.database import Database
from ...db.repository import Repository
from ..theme import palette
from ..widgets.common import Card, number, title_block


class SettingsPage(QWidget):
    """Ajustes de detecção, desempenho, segurança e privacidade."""

    statusMessage = Signal(str)
    regroupRequested = Signal()
    themeChanged = Signal(str)

    def __init__(self, repo: Repository, db: Database, settings: Settings, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.repo = repo
        self.db = db
        self.settings = settings
        self._colors = palette(theme)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 16)
        outer.setSpacing(12)
        outer.addWidget(title_block("Configurações", "Todos os ajustes ficam neste computador, em um arquivo JSON local."))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(14)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        layout.addWidget(self._build_detection_card())
        layout.addWidget(self._build_quality_card())
        layout.addWidget(self._build_performance_card())
        layout.addWidget(self._build_safety_card())
        layout.addWidget(self._build_privacy_card())
        layout.addWidget(self._build_maintenance_card())
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        restore = QPushButton("Restaurar padrões")
        restore.setObjectName("Ghost")
        restore.clicked.connect(self._restore_defaults)
        footer.addWidget(restore)
        save = QPushButton("Salvar configurações")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        footer.addWidget(save)
        outer.addLayout(footer)

    # ------------------------------------------------------------ detecção
    def _build_detection_card(self) -> Card:
        card = Card(self)
        title = QLabel("Detecção de duplicatas")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        hint = QLabel(
            "Os limites definem como o aplicativo classifica cada par de fotos. "
            "Quanto mais alto o limite de duplicata, mais conservador o resultado."
        )
        hint.setObjectName("StatLabel")
        hint.setWordWrap(True)
        card.layout().addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        thr = self.settings.thresholds
        self.duplicate_spin = _percent_spin(thr.duplicate_min)
        self.very_spin = _percent_spin(thr.very_similar_min)
        self.similar_spin = _percent_spin(thr.similar_min)
        form.addRow("Duplicata visual a partir de", self.duplicate_spin)
        form.addRow("Muito semelhante a partir de", self.very_spin)
        form.addRow("Semelhante a partir de", self.similar_spin)

        self.strict_check = QCheckBox(
            "Modo conservador (usa EXIF e proporção para evitar falsos positivos em fotos em sequência)"
        )
        self.strict_check.setChecked(self.settings.strict_mode)
        form.addRow("", self.strict_check)

        self.crops_check = QCheckBox("Detectar fotos recortadas")
        self.crops_check.setChecked(self.settings.detect_crops)
        form.addRow("", self.crops_check)

        self.embeddings_check = QCheckBox("Usar descritor visual avançado (recomendado)")
        self.embeddings_check.setChecked(self.settings.use_embeddings)
        form.addRow("", self.embeddings_check)

        onnx_row = QHBoxLayout()
        self.onnx_edit = QLineEdit(self.settings.onnx_model_path)
        self.onnx_edit.setPlaceholderText("Opcional: caminho de um modelo ONNX local")
        onnx_row.addWidget(self.onnx_edit, 1)
        browse = QPushButton("Procurar")
        browse.clicked.connect(self._pick_onnx)
        onnx_row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(onnx_row)
        form.addRow("Modelo de IA local", holder)
        card.layout().addLayout(form)

        regroup = QPushButton("Reagrupar com os novos limites (sem reanalisar as fotos)")
        regroup.clicked.connect(self._regroup)
        card.layout().addWidget(regroup)
        return card

    def _build_quality_card(self) -> Card:
        card = Card(self)
        title = QLabel("Detecção de fotos com problema")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.bad_check = QCheckBox("Analisar qualidade das fotos (desfoque, exposição, prints)")
        self.bad_check.setChecked(self.settings.analyze_bad_photos)
        form.addRow("", self.bad_check)

        self.blur_spin = QDoubleSpinBox()
        self.blur_spin.setMaximumWidth(140)
        self.blur_spin.setRange(0, 100)
        self.blur_spin.setValue(self.settings.blur_threshold)
        form.addRow("Considerar desfocada abaixo de (nitidez 0-100)", self.blur_spin)

        self.dark_spin = QDoubleSpinBox()
        self.dark_spin.setMaximumWidth(140)
        self.dark_spin.setRange(0, 255)
        self.dark_spin.setValue(self.settings.dark_threshold)
        form.addRow("Considerar escura abaixo de (luminância 0-255)", self.dark_spin)

        self.bright_spin = QDoubleSpinBox()
        self.bright_spin.setMaximumWidth(140)
        self.bright_spin.setRange(0, 255)
        self.bright_spin.setValue(self.settings.bright_threshold)
        form.addRow("Considerar superexposta acima de", self.bright_spin)
        card.layout().addLayout(form)
        return card

    def _build_performance_card(self) -> Card:
        card = Card(self)
        title = QLabel("Desempenho")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.workers_spin = QSpinBox()
        self.workers_spin.setMaximumWidth(140)
        self.workers_spin.setRange(0, 64)
        self.workers_spin.setValue(self.settings.workers)
        self.workers_spin.setToolTip("0 = automático (deixa um núcleo livre para o sistema)")
        form.addRow(f"Processos paralelos (detectado: {os.cpu_count()} núcleos)", self.workers_spin)

        self.batch_spin = QSpinBox()
        self.batch_spin.setMaximumWidth(140)
        self.batch_spin.setRange(8, 1000)
        self.batch_spin.setValue(self.settings.batch_size)
        form.addRow("Fotos por lote gravado no banco", self.batch_spin)

        self.thumb_spin = QSpinBox()
        self.thumb_spin.setMaximumWidth(140)
        self.thumb_spin.setRange(120, 640)
        self.thumb_spin.setSingleStep(20)
        self.thumb_spin.setValue(self.settings.thumbnail_size)
        form.addRow("Tamanho das miniaturas (px)", self.thumb_spin)

        self.long_side_spin = QSpinBox()
        self.long_side_spin.setMaximumWidth(140)
        self.long_side_spin.setRange(512, 2048)
        self.long_side_spin.setSingleStep(128)
        self.long_side_spin.setValue(self.settings.analysis_long_side)
        self.long_side_spin.setToolTip(
            "Resolução usada internamente na análise. Valores maiores são mais precisos e mais lentos."
        )
        form.addRow("Resolução de análise (lado maior)", self.long_side_spin)

        self.gpu_check = QCheckBox("Usar GPU quando disponível (somente com modelo ONNX)")
        self.gpu_check.setChecked(self.settings.use_gpu_if_available)
        form.addRow("", self.gpu_check)
        card.layout().addLayout(form)

        formats = imaging.supported_formats()
        status = ", ".join(f"{name}: {'sim' if ok else 'não'}" for name, ok in formats.items())
        support = QLabel("Suporte a formatos — " + status)
        support.setObjectName("StatLabel")
        support.setWordWrap(True)
        card.layout().addWidget(support)
        return card

    def _build_safety_card(self) -> Card:
        card = Card(self)
        title = QLabel("Segurança dos arquivos")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        note = QLabel(
            "O aplicativo nunca apaga, move ou modifica fotos sem confirmação. Durante a análise tudo é "
            "somente leitura: nomes, datas, EXIF e estrutura de pastas permanecem intactos."
        )
        note.setObjectName("StatLabel")
        note.setWordWrap(True)
        card.layout().addWidget(note)

        form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(360)
        self.mode_combo.addItem("Mover para a pasta de quarentena (recomendado)", "quarantine")
        self.mode_combo.addItem("Enviar para a Lixeira do sistema", "trash")
        self.mode_combo.setCurrentIndex(0 if self.settings.deletion_mode == "quarantine" else 1)
        if not HAS_SEND2TRASH:
            self.mode_combo.model().item(1).setEnabled(False)
        form.addRow("Destino dos arquivos removidos", self.mode_combo)

        quarantine_row = QHBoxLayout()
        self.quarantine_edit = QLineEdit(str(self.settings.quarantine_path()))
        quarantine_row.addWidget(self.quarantine_edit, 1)
        pick = QPushButton("Escolher")
        pick.clicked.connect(self._pick_quarantine)
        quarantine_row.addWidget(pick)
        holder = QWidget()
        holder.setLayout(quarantine_row)
        form.addRow("Pasta de quarentena", holder)

        self.structure_check = QCheckBox("Preservar a estrutura de pastas dentro da quarentena")
        self.structure_check.setChecked(self.settings.keep_folder_structure_in_quarantine)
        form.addRow("", self.structure_check)

        self.export_plan_check = QCheckBox("Oferecer exportação da lista antes de remover")
        self.export_plan_check.setChecked(self.settings.export_plan_before_action)
        form.addRow("", self.export_plan_check)
        card.layout().addLayout(form)

        row = QHBoxLayout()
        self.quarantine_info = QLabel("")
        self.quarantine_info.setObjectName("StatLabel")
        row.addWidget(self.quarantine_info, 1)
        empty = QPushButton("Esvaziar quarentena definitivamente")
        empty.setObjectName("Danger")
        empty.clicked.connect(self._empty_quarantine)
        row.addWidget(empty)
        card.layout().addLayout(row)
        self._refresh_quarantine_info()
        return card

    def _build_privacy_card(self) -> Card:
        card = Card(self)
        title = QLabel("🔒  Privacidade")
        title.setStyleSheet(f"color: {self._colors['success']}; font-weight: 700; font-size: 15px;")
        card.layout().addWidget(title)

        text = QLabel(
            "<b>Suas fotos são analisadas localmente neste computador. Nenhuma imagem é enviada para a internet.</b>"
            "<br><br>"
            "• O aplicativo funciona totalmente offline e não usa serviços de nuvem.<br>"
            "• O banco de dados, as miniaturas e os relatórios ficam no seu perfil de usuário.<br>"
            "• Nenhuma funcionalidade externa está ativa. Caso alguma venha a existir, ela será opcional e "
            "explicará qual serviço seria usado, quais dados seriam enviados, por que são necessários e "
            "como desativá-la.<br>"
            "• A localização (GPS) do EXIF só é exibida se você ativar a opção abaixo."
        )
        text.setWordWrap(True)
        card.layout().addWidget(text)

        self.gps_check = QCheckBox("Exibir coordenadas de GPS nas fichas técnicas")
        self.gps_check.setChecked(self.settings.show_gps_in_ui)
        card.layout().addWidget(self.gps_check)

        self.external_check = QCheckBox("Permitir funcionalidades que usem serviços externos (nenhuma disponível)")
        self.external_check.setChecked(self.settings.allow_external_services)
        self.external_check.setEnabled(False)
        card.layout().addWidget(self.external_check)
        return card

    def _build_maintenance_card(self) -> Card:
        card = Card(self)
        title = QLabel("Aparência e manutenção")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        form = QFormLayout()
        self.theme_combo = QComboBox()
        self.theme_combo.setMaximumWidth(180)
        self.theme_combo.addItem("Escuro", "dark")
        self.theme_combo.addItem("Claro", "light")
        self.theme_combo.setCurrentIndex(0 if self.settings.theme == "dark" else 1)
        form.addRow("Tema", self.theme_combo)
        card.layout().addLayout(form)

        self.db_label = QLabel("")
        self.db_label.setObjectName("StatLabel")
        card.layout().addWidget(self.db_label)

        row = QHBoxLayout()
        compact = QPushButton("Compactar banco de dados")
        compact.clicked.connect(self._vacuum)
        row.addWidget(compact)

        clear_decisions = QPushButton("Limpar decisões “não são duplicatas”")
        clear_decisions.setObjectName("Ghost")
        clear_decisions.clicked.connect(self._clear_decisions)
        row.addWidget(clear_decisions)

        clear_results = QPushButton("Apagar resultados (mantém o cache de análise)")
        clear_results.setObjectName("Ghost")
        clear_results.clicked.connect(self._clear_results)
        row.addWidget(clear_results)
        row.addStretch(1)
        card.layout().addLayout(row)
        self._refresh_db_info()
        return card

    # -------------------------------------------------------------- ações
    def _pick_onnx(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Escolher modelo ONNX local", "", "Modelos ONNX (*.onnx)")
        if path:
            self.onnx_edit.setText(path)

    def _pick_quarantine(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Escolher pasta de quarentena", self.quarantine_edit.text())
        if path:
            self.quarantine_edit.setText(path)

    def _refresh_quarantine_info(self) -> None:
        manager = FileManager(self.repo, self.quarantine_edit.text())
        count, total = manager.quarantine_size()
        self.quarantine_info.setText(f"Quarentena atual: {number(count)} arquivo(s), {format_bytes(total)}")

    def _refresh_db_info(self) -> None:
        counts = self.db.counts()
        self.db_label.setText(
            f"Banco local: {self.db.path}  ·  {format_bytes(self.db.size_bytes())}  ·  "
            f"{number(counts['photos'])} fotos em cache, {number(counts['groups'])} grupos"
        )

    def _empty_quarantine(self) -> None:
        manager = FileManager(self.repo, self.quarantine_edit.text())
        count, total = manager.quarantine_size()
        if count == 0:
            QMessageBox.information(self, "Quarentena", "A quarentena já está vazia.")
            return
        answer = QMessageBox.warning(
            self,
            "Esvaziar quarentena",
            f"Isto apaga definitivamente {number(count)} arquivo(s) ({format_bytes(total)}).\n\n"
            "Esta ação NÃO pode ser desfeita. Deseja continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = manager.empty_quarantine(confirmed=True)
        self._refresh_quarantine_info()
        self.statusMessage.emit(f"Quarentena esvaziada ({removed} item(ns) apagados).")

    def _vacuum(self) -> None:
        self.db.vacuum()
        self._refresh_db_info()
        self.statusMessage.emit("Banco de dados compactado.")

    def _clear_decisions(self) -> None:
        self.repo.clear_decisions()
        self.statusMessage.emit("Decisões “não são duplicatas” apagadas.")

    def _clear_results(self) -> None:
        answer = QMessageBox.question(
            self,
            "Apagar resultados",
            "Os grupos encontrados serão apagados. As fotos e o cache de análise não são afetados.\n\nContinuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repo.clear_results()
            self._refresh_db_info()
            self.statusMessage.emit("Resultados apagados.")

    def _regroup(self) -> None:
        self.save(silent=True)
        self.regroupRequested.emit()

    def _restore_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restaurar padrões",
            "Todas as configurações voltarão ao padrão (as pastas escolhidas são mantidas).\n\nContinuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        folders = list(self.settings.folders)
        defaults = Settings()
        defaults.folders = folders
        for field_name, value in defaults.to_dict().items():
            if field_name == "thresholds":
                continue
            setattr(self.settings, field_name, value)
        self.settings.thresholds = defaults.thresholds
        self.settings.save()
        self.statusMessage.emit("Configurações restauradas. Reabra a tela para ver os valores padrão.")

    def save(self, silent: bool = False) -> None:
        s = self.settings
        s.thresholds.duplicate_min = self.duplicate_spin.value()
        s.thresholds.very_similar_min = self.very_spin.value()
        s.thresholds.similar_min = self.similar_spin.value()
        s.thresholds = s.thresholds.normalized()
        s.strict_mode = self.strict_check.isChecked()
        s.detect_crops = self.crops_check.isChecked()
        s.use_embeddings = self.embeddings_check.isChecked()
        s.onnx_model_path = self.onnx_edit.text().strip()

        s.analyze_bad_photos = self.bad_check.isChecked()
        s.blur_threshold = self.blur_spin.value()
        s.dark_threshold = self.dark_spin.value()
        s.bright_threshold = self.bright_spin.value()

        s.workers = self.workers_spin.value()
        s.batch_size = self.batch_spin.value()
        s.thumbnail_size = self.thumb_spin.value()
        s.analysis_long_side = self.long_side_spin.value()
        s.use_gpu_if_available = self.gpu_check.isChecked()

        s.deletion_mode = self.mode_combo.currentData()
        s.quarantine_dir = self.quarantine_edit.text().strip()
        s.keep_folder_structure_in_quarantine = self.structure_check.isChecked()
        s.export_plan_before_action = self.export_plan_check.isChecked()

        s.show_gps_in_ui = self.gps_check.isChecked()

        new_theme = self.theme_combo.currentData()
        theme_changed = new_theme != s.theme
        s.theme = new_theme
        s.save()

        # Reflete os limites normalizados na tela.
        self.duplicate_spin.setValue(s.thresholds.duplicate_min)
        self.very_spin.setValue(s.thresholds.very_similar_min)
        self.similar_spin.setValue(s.thresholds.similar_min)
        self._refresh_quarantine_info()
        if theme_changed:
            self.themeChanged.emit(new_theme)
        if not silent:
            self.statusMessage.emit("Configurações salvas.")


def _percent_spin(value: float) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setMaximumWidth(140)
    spin.setRange(30.0, 100.0)
    spin.setDecimals(1)
    spin.setSingleStep(0.5)
    spin.setSuffix(" %")
    spin.setValue(value)
    return spin
