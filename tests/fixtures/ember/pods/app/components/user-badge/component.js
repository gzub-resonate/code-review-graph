import Component from '@glimmer/component';
import { service } from '@ember/service';

export default class UserBadge extends Component {
  @service sessionState;

  logout = () => this.sessionState.signOut();
}
