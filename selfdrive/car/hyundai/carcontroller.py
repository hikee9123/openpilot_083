from cereal import car, log
from common.realtime import DT_CTRL
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfahda_mfc, create_mdps12
from selfdrive.car.hyundai.values import Buttons, CarControllerParams, CAR, FEATURES
from opendbc.can.packer import CANPacker
from selfdrive.config import Conversions as CV
from common.numpy_fast import interp

# speed controller
from selfdrive.car.hyundai.spdcontroller  import SpdController
from selfdrive.car.hyundai.spdctrlSlow  import SpdctrlSlow
from selfdrive.car.hyundai.spdctrlNormal  import SpdctrlNormal

from common.params import Params
import common.log as trace1
import common.CTime1000 as tm
import common.MoveAvg as moveavg1
import copy

VisualAlert = car.CarControl.HUDControl.VisualAlert
LaneChangeState = log.LateralPlan.LaneChangeState



class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.p = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)

    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.steer_rate_limited = False
    self.last_resume_frame = 0
    self.last_lead_distance = 0

    self.resume_cnt = 0
    self.lkas11_cnt = 0 


    self.nBlinker = 0
    #self.lane_change_torque_lower = 0
    self.steer_torque_over_timer = 0
    self.steer_torque_ratio = 1
    self.steer_torque_ratio_dir = 1

    self.dRel = 0
    self.vRel = 0

    self.movAvg = moveavg1.MoveAvg()
    self.timer1 = tm.CTime1000("time")
    self.model_speed = 0
    self.curve_speed = 0

    
    # hud
    self.hud_timer_left = 0
    self.hud_timer_right = 0
    self.hud_sys_state = 0

    self.command_cnt = 0
    self.command_load = 0
    self.params = Params()

    self.SC = SpdctrlSlow()
    self.traceCC = trace1.Loger("CarController")


  def limit_ctrl(self, value, limit, offset ):
    p_limit = offset + limit
    m_limit = offset - limit
    if value > p_limit:
        value = p_limit
    elif  value < m_limit:
        value = m_limit
    return value


  def process_hud_alert(self, enabled, c ):
    visual_alert = c.hudControl.visualAlert
    left_lane = c.hudControl.leftLaneVisible
    right_lane = c.hudControl.rightLaneVisible

    sys_warning = (visual_alert == VisualAlert.steerRequired)

    if left_lane:
      self.hud_timer_left = 100

    if right_lane:
      self.hud_timer_right = 100

    if self.hud_timer_left:
      self.hud_timer_left -= 1
 
    if self.hud_timer_right:
      self.hud_timer_right -= 1


    # initialize to no line visible
    sys_state = 1

    if self.hud_timer_left and self.hud_timer_right or sys_warning:  # HUD alert only display when LKAS status is active
      if (self.steer_torque_ratio > 0.7) and (enabled or sys_warning):
        sys_state = 3
      else:
        sys_state = 4
    elif self.hud_timer_left:
      sys_state = 5
    elif self.hud_timer_right:
      sys_state = 6

    return sys_warning, sys_state



  def atom_tune( self, v_ego_kph, cv_value ):  # cV
    self.cv_KPH = self.CP.atomTuning.cvKPH
    self.cv_BPV = self.CP.atomTuning.cvBPV
    self.cv_sMaxV  = self.CP.atomTuning.cvsMaxV
    self.cv_sdUpV = self.CP.atomTuning.cvsdUpV
    self.cv_sdDnV = self.CP.atomTuning.cvsdDnV

    self.steerMAX = []
    self.steerdUP = []
    self.steerdDN = []

    # Max
    nPos = 0
    for sCV in self.cv_BPV:  
      self.steerMAX.append( interp( cv_value, sCV, self.cv_sMaxV[nPos] ) )
      self.steerdUP.append( interp( cv_value, sCV, self.cv_sdUpV[nPos] ) )
      self.steerdDN.append( interp( cv_value, sCV, self.cv_sdDnV[nPos] ) )
      nPos += 1
      if nPos > 20:
        break

    MAX = interp( v_ego_kph, self.cv_KPH, self.steerMAX )
    UP  = interp( v_ego_kph, self.cv_KPH, self.steerdUP )
    DN  = interp( v_ego_kph, self.cv_KPH, self.steerdDN )

    #str_log1 = 'ego={:.1f} /{:.1f}/{:.1f}/{:.1f} {}'.format(v_ego_kph,  MAX, UP, DN, self.steerMAX )
    #trace1.printf2( '{}'.format( str_log1 ) )      
    return MAX, UP, DN    



  def steerParams_torque(self, CS, actuators, path_plan ):
    param = copy.copy(self.p)
    v_ego_kph = CS.out.vEgo * CV.MS_TO_KPH
    dst_steer = actuators.steer * param.STEER_MAX
    abs_angle_steers =  abs(actuators.steeringAngleDeg)        

    self.enable_time = self.timer1.sampleTime()
    if self.enable_time < 50:
      self.steer_torque_over_timer = 0
      self.steer_torque_ratio = 1
      return param, dst_steer


    nMAX, nUP, nDN = self.atom_tune( v_ego_kph, self.model_speed )
    param.STEER_MAX = min( param.STEER_MAX, nMAX)
    param.STEER_DELTA_UP = min( param.STEER_DELTA_UP, nUP)
    param.STEER_DELTA_DOWN = min( param.STEER_DELTA_DOWN, nDN )


             
    if abs(CS.out.steeringAngleDeg) >= CS.CP.maxSteeringAngleDeg: # and CS.out.steeringPressed:
      sec_mval = 0.5  # 오파 => 운전자.  (sec)
      sec_pval = 10   #  운전자 => 오파  (sec)
      self.timer1.startTime( 5000 )
      self.steer_torque_over_timer = 50
    elif self.timer1.endTime():
      sec_mval = 5    # 오파 => 운전자.  (sec)
      sec_pval = 3    #  운전자 => 오파  (sec)  
    else:
      sec_mval = 0.5  # 오파 => 운전자.  (sec)
      sec_pval = 10   #  운전자 => 오파 

    
    if path_plan.laneChangeState != LaneChangeState.off:
      self.steer_torque_over_timer = 0
    elif CS.out.leftBlinker or CS.out.rightBlinker:
      sec_mval = 0.5  # 오파 => 운전자.
      sec_pval = 10 # 운전자 => 오파  (sec)

    if CS.out.cruiseState.cruiseSwState == Buttons.CANCEL:
      self.steer_torque_over_timer = 0
    elif not CS.out.cruiseState.enabled:
      self.steer_torque_over_timer = 0
    elif v_ego_kph > 5 and abs(CS.out.steeringTorque) > 250:  #사용자 핸들 토크
      self.steer_torque_over_timer = 50
    elif self.steer_torque_over_timer:
      self.steer_torque_over_timer -= 1

    ratio_pval = 1/(100*sec_pval)
    ratio_mval = 1/(100*sec_mval)

    if self.steer_torque_over_timer:
      self.steer_torque_ratio -= ratio_mval
    else:
      self.steer_torque_ratio += ratio_pval

    if self.steer_torque_ratio < 0:
      self.steer_torque_ratio = 0
    elif self.steer_torque_ratio > 1:
      self.steer_torque_ratio = 1

    return  param, dst_steer


  def update(self, c, CS, frame, sm, CP ):
    if self.CP != CP:
      self.CP = CP

    enabled = c.enabled
    actuators = c.actuators
    pcm_cancel_cmd = c.cruiseControl.cancel

    path_plan = sm['lateralPlan']
    self.dRel, self.vRel = SpdController.get_lead( sm )
    if self.SC is not None:
      self.model_speed = self.SC.cal_model_speed(  sm, CS.out.vEgo  )
      self.curve_speed = self.SC.cal_curve_speed( sm, CS.out.vEgo, frame)
    else:
      self.model_speed =  0
      self.curve_speed = 0


    # Steering Torque
    param, dst_steer = self.steerParams_torque( CS, c.actuators, path_plan )
    new_steer = actuators.steer * param.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, param)
    self.steer_rate_limited = new_steer != apply_steer

    apply_steer_limit = param.STEER_MAX
    if self.steer_torque_ratio < 1:
      apply_steer_limit = int(self.steer_torque_ratio * param.STEER_MAX)
      apply_steer = self.limit_ctrl( apply_steer, apply_steer_limit, 0 )


    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    lkas_active = enabled   #and abs(CS.out.steeringAngleDeg) < CS.CP.maxSteeringAngleDeg

    if not lkas_active:
      apply_steer = 0

    steer_req = 1 if apply_steer else 0
    self.apply_steer_last = apply_steer

    sys_warning, self.hud_sys_state = self.process_hud_alert( lkas_active, c )

    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"] + 1
    self.lkas11_cnt %= 0x10

    can_sends = []
    can_sends.append( create_lkas11(self.packer, self.lkas11_cnt, self.car_fingerprint, apply_steer, steer_req,
                                   CS.lkas11, sys_warning, self.hud_sys_state, c ) )

    if steer_req:
      can_sends.append( create_mdps12(self.packer, frame, CS.mdps12) )

    str_log1 = 'torg:{:5.0f} dn={:.1f} up={:.1f}'.format( apply_steer, param.STEER_DELTA_DOWN, param.STEER_DELTA_UP   )
    str_log2 = 'limit={:.0f} tm={:.1f} gap={:.0f}  gas={:.1f}'.format( apply_steer_limit, self.timer1.sampleTime(), CS.cruiseGapSet, CS.out.gas  )
    trace1.printf( '{} {}'.format( str_log1, str_log2 ) )


    #str_log1 = 'Navi=S:{} P:{:.1f} CA:{} CS:{}'.format( CS.ACC_ObjStatus, CS.ACC_ObjLatPos, CS.Navi_SCC_Camera_Act, CS.Navi_SCC_Camera_Status )
    #trace1.printf( '{}'.format( str_log1 ) )

    run_speed_ctrl = CS.acc_active and self.SC != None
    if not run_speed_ctrl:
      str_log2 = 'LKAS={:.0f}  steer={:5.0f}'.format( CS.lkas_button_on,  CS.out.steeringTorque )
      trace1.printf2( '{}'.format( str_log2 ) )


    if pcm_cancel_cmd:
      can_sends.append(create_clu11(self.packer, frame, CS.clu11, Buttons.CANCEL))
    if CS.out.cruiseState.standstill:
      # run only first time when the car stopped
      if self.last_lead_distance == 0:  
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance != self.last_lead_distance and (frame - self.last_resume_frame) > 5:
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.clu11, Buttons.RES_ACCEL))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = frame
          self.resume_cnt = 0
    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0
    elif run_speed_ctrl and self.SC != None:
      is_sc_run = self.SC.update( CS, sm, self )
      if is_sc_run:
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.clu11, self.SC.btn_type, self.SC.sc_clu_speed ))
        self.resume_cnt += 1
      else:
        self.resume_cnt = 0

    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.car_fingerprint in FEATURES["use_lfa_mfa"]:
      can_sends.append(create_lfahda_mfc(self.packer, enabled, CS))

   # counter inc
    self.lkas11_cnt += 1
    return can_sends
