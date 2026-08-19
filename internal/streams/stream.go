package streams

import (
	"encoding/json"
	"sync"
	"sync/atomic"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/core"
)

type Stream struct {
	producers []*Producer
	consumers []core.Consumer
	mu        sync.Mutex
	pending   atomic.Int32

	linger         time.Duration
	stopTimer      *time.Timer
	stopGeneration uint64
}

func NewStream(source any) *Stream {
	switch source := source.(type) {
	case string:
		return &Stream{
			producers: []*Producer{NewProducer(source)},
		}
	case []string:
		s := new(Stream)
		for _, str := range source {
			s.producers = append(s.producers, NewProducer(str))
		}
		return s
	case []any:
		s := new(Stream)
		for _, src := range source {
			str, ok := src.(string)
			if !ok {
				log.Error().Msgf("[stream] NewStream: Expected string, got %v", src)
				continue
			}
			s.producers = append(s.producers, NewProducer(str))
		}
		return s
	case map[string]any:
		return NewStream(source["url"])
	case nil:
		return new(Stream)
	default:
		panic(core.Caller())
	}
}

func (s *Stream) Sources() []string {
	sources := make([]string, 0, len(s.producers))
	for _, prod := range s.producers {
		sources = append(sources, prod.url)
	}
	return sources
}

func (s *Stream) SetSource(source string) {
	for _, prod := range s.producers {
		prod.SetSource(source)
	}
}

func (s *Stream) SetLinger(duration time.Duration) {
	s.mu.Lock()
	s.linger = duration
	if duration <= 0 && s.stopTimer != nil {
		s.stopTimer.Stop()
		s.stopTimer = nil
		s.stopGeneration++
	}
	s.mu.Unlock()
}

func (s *Stream) cancelLingerStop() {
	s.mu.Lock()
	s.cancelLingerStopLocked()
	s.mu.Unlock()
}

func (s *Stream) cancelLingerStopLocked() {
	if s.stopTimer != nil {
		s.stopTimer.Stop()
		s.stopTimer = nil
		s.stopGeneration++
	}
}

func (s *Stream) beginConsumerAdd() int32 {
	s.mu.Lock()
	consN := s.pending.Add(1) - 1
	s.cancelLingerStopLocked()
	s.mu.Unlock()
	return consN
}

func (s *Stream) scheduleStopProducers() {
	s.mu.Lock()
	if len(s.consumers) != 0 || s.linger <= 0 {
		s.mu.Unlock()
		s.stopProducers()
		return
	}

	if s.stopTimer != nil {
		s.stopTimer.Stop()
	}
	s.stopGeneration++
	generation := s.stopGeneration
	delay := s.linger
	s.stopTimer = time.AfterFunc(delay, func() {
		s.mu.Lock()
		if s.stopGeneration != generation {
			s.mu.Unlock()
			return
		}
		s.stopTimer = nil

		log.Debug().Dur("linger", delay).Msg("[streams] linger expired")
		s.stopProducersLocked()
		s.mu.Unlock()
	})
	s.mu.Unlock()

	log.Debug().Dur("linger", delay).Msg("[streams] keep producers warm")
}

func (s *Stream) RemoveConsumer(cons core.Consumer) {
	_ = cons.Stop()

	s.mu.Lock()
	for i, consumer := range s.consumers {
		if consumer == cons {
			s.consumers = append(s.consumers[:i], s.consumers[i+1:]...)
			break
		}
	}
	s.mu.Unlock()

	s.scheduleStopProducers()
}

func (s *Stream) AddProducer(prod core.Producer) {
	producer := &Producer{conn: prod, state: stateExternal, url: "external"}
	s.mu.Lock()
	s.producers = append(s.producers, producer)
	s.mu.Unlock()
}

func (s *Stream) RemoveProducer(prod core.Producer) {
	s.mu.Lock()
	for i, producer := range s.producers {
		if producer.conn == prod {
			s.producers = append(s.producers[:i], s.producers[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
}

func (s *Stream) stopProducers() {
	s.mu.Lock()
	s.stopProducersLocked()
	s.mu.Unlock()
}

func (s *Stream) stopProducersLocked() {
	if s.pending.Load() > 0 {
		log.Trace().Msg("[streams] skip stop pending producer")
		return
	}

producers:
	for _, producer := range s.producers {
		for _, track := range producer.receivers {
			if len(track.Senders()) > 0 {
				continue producers
			}
		}
		for _, track := range producer.senders {
			if len(track.Senders()) > 0 {
				continue producers
			}
		}
		producer.stop()
	}
}

func (s *Stream) MarshalJSON() ([]byte, error) {
	var info = struct {
		Producers []*Producer     `json:"producers"`
		Consumers []core.Consumer `json:"consumers"`
	}{
		Producers: s.producers,
		Consumers: s.consumers,
	}
	return json.Marshal(info)
}
